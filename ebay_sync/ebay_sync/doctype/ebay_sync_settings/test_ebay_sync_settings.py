# SPDX-License-Identifier: AGPL-3.0-or-later
"""eBay Sync tests: the pure mapping rules (shapes from eBay's sell_inventory_v1 / sell_fulfillment_v1
OpenAPI specs), and the ERPNext side of the engine against the real test DB (ERPNext fixtures): paid
order → buyer + shipping address + Sales Invoice + Payment Entry, idempotency, unpaid/unmapped orders,
remitted tax, stock availability. eBay itself is exercised in the eBay sandbox (go-live gate)."""

from decimal import Decimal

import frappe
from frappe.tests.utils import FrappeTestCase

from ebay_sync import mapping, sync
from ebay_sync.ebay_api import token_request

SYNC = "_Test eBay Sync"
COMPANY = "_Test Company"
WAREHOUSE = "_Test Warehouse - _TC"


def amt(value, currency="INR"):
	return {"value": value, "currency": currency}


def paid_order(order_id="12-34567-89012", **overrides):
	order = {
		"orderId": order_id,
		"creationDate": "2026-10-04T18:00:00.000Z",
		"lastModifiedDate": "2026-10-04T18:05:00.000Z",
		"orderPaymentStatus": "PAID",
		"cancelStatus": {"cancelState": "NONE_REQUESTED"},
		"buyer": {
			"username": "buyer_one",
			"buyerRegistrationAddress": {
				"fullName": "Buyer One",
				"email": "relay@members.ebay.ca",
				"primaryPhone": {"phoneNumber": "4035550100"},
			},
		},
		"fulfillmentStartInstructions": [
			{
				"shippingStep": {
					"shipTo": {
						"fullName": "Buyer One",
						"contactAddress": {
							"addressLine1": "1 Main St",
							"city": "Calgary",
							"stateOrProvince": "AB",
							"postalCode": "T2P 1J9",
							"countryCode": "CA",
						},
					}
				}
			}
		],
		"lineItems": [
			{
				"lineItemId": "L1",
				"sku": "_Test Item",
				"quantity": 2,
				"lineItemCost": amt("30.00"),
				"title": "Test item",
			}
		],
		"pricingSummary": {
			"priceSubtotal": amt("30.00"),
			"deliveryCost": amt("5.00"),
			"tax": amt("2.10"),
			"total": amt("37.10"),
		},
		"ebayCollectAndRemitTax": True,
		"totalMarketplaceFee": amt("4.50"),
	}
	order.update(overrides)
	return order


class TestMapping(FrappeTestCase):
	def test_token_request_is_basic_auth_refresh_grant(self):
		headers, form = token_request("APP-ID", "CERT-ID", "v^1.1#refresh")
		self.assertEqual(headers["Authorization"], "Basic QVBQLUlEOkNFUlQtSUQ=")
		self.assertEqual(form["grant_type"], "refresh_token")
		self.assertIn("sell.inventory", form["scope"])
		self.assertIn("sell.fulfillment", form["scope"])

	def test_inventory_item_and_group(self):
		item = {"item_code": "TEE", "item_name": "T" * 100, "description": "Shirt"}
		body = mapping.inventory_item(item, Decimal("3"), ["https://erp/x.jpg"], "NEW", {"Size": ["S"]})
		self.assertEqual(len(body["product"]["title"]), mapping.TITLE_MAX)
		self.assertEqual(body["availability"]["shipToLocationAvailability"]["quantity"], 3)
		self.assertEqual(body["product"]["aspects"], {"Size": ["S"]})
		group = mapping.item_group(
			item,
			[
				{"item_code": "TEE-S", "attributes": [("Size", "S"), ("Colour", "Pink")]},
				{"item_code": "TEE-M", "attributes": [("Size", "M"), ("Colour", "Pink")]},
			],
			[],
		)
		self.assertEqual(group["variantSKUs"], ["TEE-S", "TEE-M"])
		self.assertEqual(
			group["variesBy"]["specifications"],
			[{"name": "Size", "values": ["S", "M"]}, {"name": "Colour", "values": ["Pink"]}],
		)

	def test_offer_and_price_quantity(self):
		settings = {
			"marketplace_id": "EBAY_CA",
			"category_id": "123",
			"merchant_location_key": "HOME",
			"fulfillment_policy_id": "F",
			"payment_policy_id": "P",
			"return_policy_id": "R",
		}
		body = mapping.offer("SKU1", settings, "19.5", "cad", Decimal("4"))
		self.assertEqual(
			(body["format"], body["marketplaceId"], body["availableQuantity"]), ("FIXED_PRICE", "EBAY_CA", 4)
		)
		self.assertEqual(body["pricingSummary"]["price"], {"value": "19.50", "currency": "CAD"})
		self.assertEqual(
			body["listingPolicies"],
			{"fulfillmentPolicyId": "F", "paymentPolicyId": "P", "returnPolicyId": "R"},
		)
		request = mapping.price_quantity("SKU1", Decimal("2"), "OFFER1", 20, "CAD")
		self.assertEqual(
			request["offers"],
			[{"offerId": "OFFER1", "availableQuantity": 2, "price": {"value": "20.00", "currency": "CAD"}}],
		)
		self.assertNotIn("offers", mapping.price_quantity("SKU1", 2))

	def test_order_rules(self):
		self.assertTrue(mapping.is_importable(paid_order()))
		self.assertFalse(mapping.is_importable(paid_order(orderPaymentStatus="PENDING")))
		self.assertFalse(mapping.is_importable(paid_order(cancelStatus={"cancelState": "CANCELED"})))
		self.assertEqual(
			mapping.invoice_lines(paid_order()),
			[{"item_code": "_Test Item", "qty": 2.0, "rate": 15.0, "description": "Test item"}],
		)
		totals = mapping.invoice_totals(paid_order())
		self.assertEqual(
			(totals["shipping"], totals["tax"], totals["fee"]), (Decimal("5.00"), Decimal(0), Decimal("4.50"))
		)
		self.assertEqual(
			mapping.invoice_totals(paid_order(ebayCollectAndRemitTax=False))["tax"], Decimal("2.10")
		)
		with self.assertRaises(KeyError):
			mapping.invoice_lines(
				paid_order(lineItems=[{"lineItemId": "L9", "quantity": 1, "lineItemCost": amt("1")}])
			)

	def test_buyer_and_address(self):
		self.assertEqual(mapping.buyer_values(paid_order())["customer_name"], "Buyer One")
		self.assertEqual(mapping.shipping_address(paid_order())["city"], "Calgary")
		self.assertIsNone(mapping.shipping_address(paid_order(fulfillmentStartInstructions=[])))
		self.assertEqual(mapping.available_quantity(5, 7), Decimal(0))


class TestEbaySyncEngine(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		if not frappe.db.exists("eBay Sync Settings", SYNC):
			frappe.get_doc(
				{
					"doctype": "eBay Sync Settings",
					"sync_name": SYNC,
					"enabled": 0,
					"company": COMPANY,
					"environment": "Sandbox",
					"marketplace_id": "EBAY_CA",
					"client_id": "APP",
					"client_secret": "CERT",
					"refresh_token": "REFRESH",
					"item_group": "_Test Item Group",
					"price_list": "_Test Price List",
					"warehouse": WAREHOUSE,
					"customer_group": "_Test Customer Group",
					"mode_of_payment": "Cash",
					"deposit_account": "_Test Bank - _TC",
					"fee_account": "_Test Account Cost for Goods Sold - _TC",
					"shipping_account": "_Test Account Shipping Charges - _TC",
					"tax_account": "_Test Account VAT - _TC",
				}
			).insert()
		self.settings = frappe.get_doc("eBay Sync Settings", SYNC)

	def tearDown(self):
		frappe.db.rollback()

	def test_settings_reject_other_company_account(self):
		self.settings.fee_account = "_Test Account Cost for Goods Sold - _TC1"
		with self.assertRaises(frappe.ValidationError):
			self.settings.save()

	def test_paid_order_becomes_buyer_invoice_payment(self):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		make_stock_entry(item_code="_Test Item", target=WAREHOUSE, qty=5, basic_rate=10)
		order = paid_order()
		self.assertTrue(sync.import_order(self.settings, order))
		invoice = frappe.get_doc(
			"Sales Invoice", sync.erpnext_name_for(self.settings, "order", order["orderId"])
		)
		self.assertEqual((invoice.docstatus, invoice.update_stock, invoice.po_no), (1, 1, order["orderId"]))
		self.assertEqual(invoice.grand_total, 35.0)  # 30 items + 5 shipping; remitted tax left out
		self.assertEqual(invoice.outstanding_amount, 0)
		customer = sync.erpnext_name_for(self.settings, "buyer", "buyer_one")
		self.assertEqual(invoice.customer, customer)
		self.assertTrue(
			frappe.get_all("Dynamic Link", filters={"link_name": customer, "parenttype": "Address"})
		)
		pe = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_name": invoice.name, "docstatus": 1},
			pluck="parent",
		)
		self.assertEqual(
			frappe.get_all("Payment Entry Deduction", filters={"parent": pe[0]}, pluck="amount"), [4.5]
		)
		self.assertTrue(sync.import_order(self.settings, order))  # idempotent
		# the engine commits per order, so count this order's links only (not other runs' orders)
		self.assertEqual(
			frappe.db.count(
				"eBay Sync Link", {"settings": SYNC, "kind": "order", "ebay_id": order["orderId"]}
			),
			1,
		)
		# a second order from the same buyer reuses the customer and doesn't duplicate the address
		self.assertTrue(sync.import_order(self.settings, paid_order("12-34567-89013")))
		self.assertEqual(
			frappe.db.count("eBay Sync Link", {"settings": SYNC, "kind": "buyer", "ebay_id": "buyer_one"}), 1
		)
		self.assertEqual(
			len(frappe.get_all("Dynamic Link", filters={"link_name": customer, "parenttype": "Address"})), 1
		)

	def test_unpaid_order_is_skipped_and_unmapped_is_held(self):
		self.assertTrue(sync.import_order(self.settings, paid_order("UNPAID", orderPaymentStatus="PENDING")))
		self.assertIsNone(sync.erpnext_name_for(self.settings, "order", "UNPAID"))
		held = paid_order("NOSKU", lineItems=[{"lineItemId": "L9", "quantity": 1, "lineItemCost": amt("1")}])
		self.assertFalse(sync.import_order(self.settings, held))
		self.assertIsNone(sync.erpnext_name_for(self.settings, "order", "NOSKU"))

	def test_available_reads_the_sync_warehouse(self):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		make_stock_entry(item_code="_Test Item", target=WAREHOUSE, qty=3, basic_rate=10)
		actual, reserved = frappe.db.get_value(
			"Bin", {"item_code": "_Test Item", "warehouse": WAREHOUSE}, ["actual_qty", "reserved_qty"]
		)
		result = sync.available(self.settings, ["_Test Item", "_Test Item With No Stock"])
		self.assertEqual(result["_Test Item"], mapping.available_quantity(actual, reserved))
		self.assertEqual(result["_Test Item With No Stock"], 0)
