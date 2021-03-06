# Copyright (c) 2013, Saurabh and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe import _
import time
import itertools
from frappe.utils import getdate, add_months, nowdate
from frappe.utils.data import today, nowtime, cint, cstr, flt
from loyalty_point_engine.loyalty_point_engine.doctype.rule.rule import get_vsibility_setting
from loyalty_point_engine.loyalty_point_engine.accounts_handler import create_jv, get_payable_acc, get_marketing_account

def initiate_point_engine(journal_voucher, sales_invoice_details):
	valid_rules = get_applicable_rule()
	rule_details = get_ruel_details(valid_rules)
	if rule_details:
		calulate_points(rule_details, journal_voucher, sales_invoice_details)

def get_applicable_rule():
	rule_validity_checks_param = {}

	for rule in frappe.db.sql("select name from tabRule where is_active = 1 ",as_list=1):
		get_configurations(rule[0], rule_validity_checks_param)

	return check_validity(rule_validity_checks_param)

def get_configurations(rule_type, rule_validity):
	rule_validity[rule_type] = get_vsibility_setting(rule_type, only_visble_fields=1)

def check_validity(rule_validity_checks_param):
	valid_rules = []
	for rule in rule_validity_checks_param:
		rules = frappe.db.sql("select name from tabRule where is_active = 1 %s "%make_cond(rule_validity_checks_param[rule]), as_list=1)
		valid_rules.append(list(itertools.chain(*rules)))
	rules = None
	return list(itertools.chain(*valid_rules))

def make_cond(validity_list):
	cond_list = []
	for param in validity_list:
		if 'from_date' in param:
			cond_list.append(" %s <= '%s' "%(param, today()))
		if 'to_date' in param:
			cond_list.append(" %s >= '%s' "%(param, today()))
		if 'start_time' in param:
			cond_list.append(" %s <= '%s' "%(param, nowtime()))
		if 'end_time' in param:
			cond_list.append(" %s >= '%s' "%(param, nowtime()))
			
	return ' and ' + ' and '.join(cond_list)

def get_ruel_details(rules):
	rule_details = {}
	for rule in rules:
		rule_details[rule] = frappe.db.sql(""" select amount, points_earned, is_lp_mumtiplier, referred_points, 
			multiplier, ifnull(group_concat(pm.mode),'') as payment_modes, ifnull(transaction_limit, 999999999) as transaction_limit, ifnull(valid_upto, '6') as valid_upto
			from `tabRule` r, `tabPayment Modes` pm 
			where r.name = '%(rule_name)s' 
			and pm.parent = '%(rule_name)s'"""%{'rule_name': rule}, as_dict=1)[0]
	return rule_details

def calulate_points(rule_details, journal_voucher, sales_invoice_details):
	points_earned = 0
	referral_points = 0
	valid_modes = []
	debit_to, credit_to = get_accouts(sales_invoice_details.customer, sales_invoice_details.company)
	for rule in rule_details:
		rule_based_points = 0
		valid_modes = valid_payment_modes(rule_details[rule], journal_voucher)
		if valid_modes:
			rule_based_points += calc_basic_points(rule_details[rule], something(valid_modes, journal_voucher))
			if rule_details[rule].get('is_lp_mumtiplier') == 1:
				rule_based_points = multiplier_points(rule_details[rule], rule_based_points)
			make_point_entry(rule_based_points, rule_details[rule], sales_invoice_details, debit_to, credit_to)

		if within_referral_count(sales_invoice_details, rule_details[rule]) == 1:
			referral_points += calc_referral_points(rule_details[rule])
		points_earned += rule_based_points

	create_reddem_points_entry(rule_details, sales_invoice_details, debit_to, credit_to, journal_voucher)
	make_referred_points_entry(sales_invoice_details, referral_points)

def valid_payment_modes(rule_details, journal_voucher):
	modes = get_applied_payment_modes(journal_voucher.entries)
	return check_modes(rule_details, modes)

def get_applied_payment_modes(payment_details):
	modes = []
	for payment_type in payment_details:
		modes.append(payment_type.mode)
	return modes

def check_modes(rule_details, mode_of_payment):
	return set(rule_details.payment_modes.split(',')).intersection(mode_of_payment)

def calc_basic_points(rule_details, inv_amount):
	return rule_details.get('points_earned')*cint(inv_amount/rule_details.get('amount'))

def multiplier_points(rule_details, points_earned):
	return points_earned * cint(rule_details.get('multiplier'))

def within_referral_count(sales_invoice_details, rule):
	validator = frappe.db.sql(""" select if((select count(*) from `tabPoint Transaction` 
		where ref_name = '%s') < %s, true, false)  """%(sales_invoice_details.referral_name, 
		rule.transaction_limit),as_list=1)
	return ((len(validator[0]) > 1) and validator[0] or validator[0][0]) if validator else None

def calc_referral_points(rule_details):
	return cint(rule_details.get('referred_points'))

def make_point_entry(points_earned, rule_details, sales_invoice_details, debit_to, credit_to):
	create_earned_points_entry(points_earned, rule_details, sales_invoice_details, debit_to, credit_to)
	
def create_earned_points_entry(points_earned, rule_details, sales_invoice_details, debit_to, credit_to):
	create_point_transaction('Customer', sales_invoice_details.customer, sales_invoice_details.name,  'Earned', points_earned, rule_details)
	conversion_factor = frappe.db.get_value('LPE Configuration', None, 'conversion_factor')
	create_jv(sales_invoice_details, points_earned * flt(conversion_factor), debit_to, credit_to)

def create_reddem_points_entry(rule_details, sales_invoice_details, debit_to, credit_to, journal_voucher):
	debit_to, credit_to = credit_to, debit_to
	for entry in journal_voucher.entries:
		if entry.mode == "Redeem":
			conversion_factor = frappe.db.get_value('LPE Configuration', None, 'conversion_factor')
			create_point_transaction('Customer', sales_invoice_details.customer, entry.against_invoice, 'Redeem', cint(flt(entry.credit) / flt(conversion_factor)))
			create_jv(sales_invoice_details, sales_invoice_details.redeem_points, debit_to, credit_to)

def create_point_transaction(ref_link, ref_name, inv, tran_type, points, rule_details=None):
	if points != 0:
		tran = frappe.new_doc("Point Transaction")
		tran.ref_link = ref_link
		tran.ref_name = ref_name	
		tran.date = today()
		tran.type = tran_type
		tran.points = cint(points) * 1 if tran_type == 'Earned' else -1 * cint(points)
		if rule_details: 
			tran.valied_upto = add_months(nowdate(), cint(rule_details.get('valid_upto'))) 
		tran.invoice_number = inv
		tran.rule_details = cstr(rule_details)
		tran.docstatus = 1
		tran.insert()

def make_referred_points_entry(sales_invoice_details, referral_points):
	if sales_invoice_details.referral_name:
		create_point_transaction(sales_invoice_details.referral, sales_invoice_details.referral_name, sales_invoice_details.name, 'Earned', referral_points)
		debit_to, credit_to = get_accouts(sales_invoice_details.referral_name, sales_invoice_details.company)
		create_jv(sales_invoice_details, referral_points, debit_to, credit_to)

def get_accouts(party, company):
	"marketing account is debit account and customer's loyalty account is credit account"
	return get_marketing_account(company), get_payable_acc(party)

def something(valid_modes, journal_voucher):
	total = 0
	for entry in journal_voucher.entries:
		if entry.mode in valid_modes:
			total += cint(entry.credit)
	return total