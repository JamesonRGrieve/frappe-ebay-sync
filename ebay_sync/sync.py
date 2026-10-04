# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sync engine for one eBay Sync Settings. ERPNext is master for stock and prices (and, when
``create_listings`` is on, the listings themselves); paid eBay orders and their buyers are imported.
eBay Sync Link tracks offers, listings, orders and buyers, so re-runs never duplicate. eBay has no
usable order webhook for this, so orders are polled (scheduler) while stock/price changes push on
ERPNext doc events."""

from datetime import timedelta

import frappe
from frappe.utils import add_to_date, get_datetime, get_url, now_datetime

from ebay_sync import mapping
from ebay_sync.ebay_api import PRICE_QUANTITY_MAX, Client, EbayError

FIRST_SYNC_LOOKBACK_DAYS = 1
TOKEN_CACHE_PREFIX = "ebay_sync:token:"
TOKEN_MARGIN_SECONDS = 300


def client(settings):
	"""An API client with a cached access token (refreshed from the seller's refresh token)."""
	key = TOKEN_CACHE_PREFIX + settings.name
	token = frappe.cache.get_value(key)
	if not token:
		token, expires_in = Client.refresh_access_token(
			settings.environment,
			settings.client_id,
			settings.get_password("client_secret"),
			settings.get_password("refresh_token"),
		)
		frappe.cache.set_value(key, token, expires_in_sec=max(expires_in - TOKEN_MARGIN_SECONDS, 60))
	return Client(settings.environment, settings.marketplace_id, token)


def iso(dt):
	return get_datetime(dt).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── links ────────────────────────────────────────────────────────────────────────────────────────


def links(settings, kind):
	rows = frappe.get_all(
		"eBay Sync Link",
		filters={"settings": settings.name, "kind": kind},
		fields=["erpnext_name", "ebay_id"],
	)
	return {r.erpnext_name: r.ebay_id for r in rows}


def set_link(settings, kind, doctype, name, ebay_id):
	existing = frappe.db.get_value(
		"eBay Sync Link", {"settings": settings.name, "kind": kind, "erpnext_name": name}
	)
	if existing:
		frappe.db.set_value("eBay Sync Link", existing, {"ebay_id": ebay_id, "synced_at": now_datetime()})
	else:
		frappe.get_doc(
			{
				"doctype": "eBay Sync Link",
				"settings": settings.name,
				"kind": kind,
				"erpnext_doctype": doctype,
				"erpnext_name": name,
				"ebay_id": ebay_id,
				"synced_at": now_datetime(),
			}
		).insert(ignore_permissions=True)


def erpnext_name_for(settings, kind, ebay_id):
	return frappe.db.get_value(
		"eBay Sync Link", {"settings": settings.name, "kind": kind, "ebay_id": ebay_id}, "erpnext_name"
	)


# ── ERPNext side ─────────────────────────────────────────────────────────────────────────────────


def catalog_items(settings):
	lft, rgt = frappe.db.get_value("Item Group", settings.item_group, ["lft", "rgt"])
	groups = frappe.get_all("Item Group", filters={"lft": [">=", lft], "rgt": ["<=", rgt]}, pluck="name")
	return frappe.get_all(
		"Item",
		filters={
			"item_group": ["in", groups],
			"is_sales_item": 1,
			"disabled": 0,
			"variant_of": ["is", "not set"],
		},
		fields=["item_code", "item_name", "description", "has_variants", "image"],
	)


def variants_of(template):
	return [
		{
			"item_code": code,
			"attributes": [
				(a.attribute, a.attribute_value)
				for a in frappe.get_all(
					"Item Variant Attribute",
					filters={"parent": code},
					fields=["attribute", "attribute_value"],
					order_by="idx asc",
				)
			],
		}
		for code in frappe.get_all("Item", filters={"variant_of": template, "disabled": 0}, pluck="item_code")
	]


def available(settings, item_codes):
	"""{item_code: available quantity} in the sync warehouse (actual less reserved, never negative)."""
	bins = {
		b.item_code: b
		for b in frappe.get_all(
			"Bin",
			filters={"warehouse": settings.warehouse, "item_code": ["in", list(item_codes) or [""]]},
			fields=["item_code", "actual_qty", "reserved_qty"],
		)
	}
	return {
		c: (mapping.available_quantity(bins[c].actual_qty, bins[c].reserved_qty) if c in bins else 0)
		for c in item_codes
	}


def prices(settings):
	currency = frappe.db.get_value("Price List", settings.price_list, "currency")
	rates = {
		p.item_code: p.price_list_rate
		for p in frappe.get_all(
			"Item Price",
			filters={"price_list": settings.price_list, "selling": 1},
			fields=["item_code", "price_list_rate"],
		)
	}
	return rates, currency


def image_urls(item):
	return [get_url(item.image)] if item.image else []


def listing_settings(settings):
	return {
		k: settings.get(k)
		for k in (
			"marketplace_id",
			"category_id",
			"merchant_location_key",
			"fulfillment_policy_id",
			"payment_policy_id",
			"return_policy_id",
		)
	}


# ── listings, stock, prices ──────────────────────────────────────────────────────────────────────


def push_listings(settings_name):
	"""Create (and publish) eBay listings for ERPNext items that have none yet; existing offers keep
	being updated by ``push_stock_price``. Items without a price are skipped (eBay needs one)."""
	settings = frappe.get_doc("eBay Sync Settings", settings_name)
	if not settings.create_listings:
		return
	api, (rates, currency), offers = client(settings), prices(settings), links(settings, "offer")
	for item in catalog_items(settings):
		variants = variants_of(item.item_code) if item.has_variants else []
		skus = [v["item_code"] for v in variants] or [item.item_code]
		if all(s in offers for s in skus) or not all(s in rates for s in skus):
			continue
		quantities = available(settings, skus)
		try:
			for v in variants or [{"item_code": item.item_code, "attributes": []}]:
				api.put_inventory_item(
					v["item_code"],
					mapping.inventory_item(
						item,
						quantities[v["item_code"]],
						image_urls(item),
						settings.condition,
						mapping.variant_aspects(v["attributes"]),
					),
				)
			if variants:
				api.put_inventory_item_group(
					item.item_code, mapping.item_group(item, variants, image_urls(item))
				)
			for sku in skus:
				if sku not in offers:
					offers[sku] = api.create_offer(
						mapping.offer(sku, listing_settings(settings), rates[sku], currency, quantities[sku])
					)
					set_link(settings, "offer", "Item", sku, offers[sku])
			listing = (
				api.publish_item_group(item.item_code)
				if variants
				else api.publish_offer(offers[item.item_code])
			)
			set_link(settings, "listing", "Item", item.item_code, listing)
			frappe.db.commit()
		except EbayError, OSError:
			frappe.db.rollback()
			frappe.log_error(
				title=f"eBay listing for {item.item_code} failed", message=frappe.get_traceback()
			)


def push_stock_price(settings_name, item_codes=None):
	"""Set every linked offer's quantity (and price) to ERPNext's, 25 SKUs per call."""
	settings = frappe.get_doc("eBay Sync Settings", settings_name)
	offers = links(settings, "offer")
	skus = [s for s in (item_codes or offers) if s in offers]
	if not skus:
		return
	(rates, currency), quantities = prices(settings), available(settings, skus)
	requests_ = [mapping.price_quantity(s, quantities[s], offers[s], rates.get(s), currency) for s in skus]
	api = client(settings)
	for i in range(0, len(requests_), PRICE_QUANTITY_MAX):
		for response in api.bulk_update_price_quantity(requests_[i : i + PRICE_QUANTITY_MAX]):
			if int(response.get("statusCode") or 200) >= 400:
				frappe.log_error(
					title=f"eBay stock/price update failed for {response.get('sku')}",
					message=frappe.as_json(response.get("errors")),
				)


# ── orders + buyers ──────────────────────────────────────────────────────────────────────────────


def upsert_buyer(settings, order):
	username = (order.get("buyer") or {}).get("username")
	values = mapping.buyer_values(order)
	if not (username and values):
		return None
	name = erpnext_name_for(settings, "buyer", username)
	if not name:
		doc = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_group": settings.customer_group,
				"territory": settings.territory,
				**{k: v for k, v in values.items() if v},
			}
		)
		doc.insert(ignore_permissions=True)
		name = doc.name
		set_link(settings, "buyer", "Customer", name, username)
	address = mapping.shipping_address(order)
	if address:
		country = frappe.db.get_value("Country", {"code": (address.pop("country_code") or "").lower()})
		exists = frappe.db.exists(
			"Address",
			{
				"address_line1": address["address_line1"],
				"pincode": address.get("pincode"),
				"name": [
					"in",
					frappe.get_all(
						"Dynamic Link",
						filters={"link_doctype": "Customer", "link_name": name, "parenttype": "Address"},
						pluck="parent",
					)
					or [""],
				],
			},
		)
		if country and not exists:
			frappe.get_doc(
				{
					"doctype": "Address",
					"address_type": "Shipping",
					"country": country,
					**{k: v for k, v in address.items() if v},
					"address_title": address.get("address_title") or values["customer_name"],
					"links": [{"link_doctype": "Customer", "link_name": name}],
				}
			).insert(ignore_permissions=True)
	return name


def make_invoice(settings, order, customer):
	totals = mapping.invoice_totals(order)
	created = get_datetime(order["creationDate"]).replace(tzinfo=None)
	invoice = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"company": settings.company,
			"customer": customer,
			"set_posting_time": 1,
			"posting_date": created.date(),
			"posting_time": created.time(),
			"update_stock": 1,
			"set_warehouse": settings.warehouse,
			"cost_center": settings.cost_center,
			"po_no": order["orderId"],
			"remarks": f"eBay order {order['orderId']}",
			"items": [
				dict(row, warehouse=settings.warehouse, cost_center=settings.cost_center)
				for row in mapping.invoice_lines(order)
			],
			"apply_discount_on": "Net Total",
			"discount_amount": float(totals["discount"]),
			"taxes": [
				{
					"charge_type": "Actual",
					"account_head": account,
					"description": label,
					"tax_amount": float(value),
					"cost_center": settings.cost_center,
				}
				for label, account, value in (
					("eBay shipping", settings.shipping_account, totals["shipping"]),
					("Sales tax", settings.tax_account, totals["tax"]),
				)
				if value
			],
		}
	)
	invoice.insert(ignore_permissions=True)
	invoice.submit()
	return invoice, totals


def make_payment(settings, invoice, totals, order):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	payment = get_payment_entry("Sales Invoice", invoice.name)
	payment.mode_of_payment = settings.mode_of_payment
	payment.paid_to = settings.deposit_account
	payment.reference_no, payment.reference_date = order["orderId"], invoice.posting_date
	if totals["fee"]:
		payment.append(
			"deductions",
			{
				"account": settings.fee_account,
				"amount": float(totals["fee"]),
				"cost_center": settings.cost_center
				or frappe.get_cached_value("Company", settings.company, "cost_center"),
				"description": "eBay fees",
			},
		)
		payment.received_amount = payment.paid_amount = float(invoice.grand_total) - float(totals["fee"])
		payment.set_amounts()
	payment.insert(ignore_permissions=True)
	payment.submit()
	return payment


def import_order(settings, order):
	"""Import one eBay order (idempotent). True if imported or skipped by design (unpaid/cancelled)."""
	if erpnext_name_for(settings, "order", order["orderId"]) or not mapping.is_importable(order):
		return True
	try:
		customer = upsert_buyer(settings, order)
		if not customer:
			raise ValueError(f"eBay order {order['orderId']} has no buyer")
		invoice, totals = make_invoice(settings, order, customer)
		make_payment(settings, invoice, totals, order)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"eBay order {order['orderId']} not imported", message=frappe.get_traceback())
		return False
	set_link(settings, "order", "Sales Invoice", invoice.name, order["orderId"])
	frappe.db.commit()
	return True


def import_orders(settings_name):
	"""Import paid orders modified since the cursor; the cursor only passes cleanly imported orders."""
	settings = frappe.get_doc("eBay Sync Settings", settings_name)
	since = settings.orders_synced_until or add_to_date(now_datetime(), days=-FIRST_SYNC_LOOKBACK_DAYS)
	api, offset, synced_until = client(settings), 0, get_datetime(since)
	while True:
		page = api.get_orders(iso(since), offset)
		orders = sorted(page.get("orders") or [], key=lambda o: o["lastModifiedDate"])
		for order in orders:
			if not import_order(settings, order):
				settings.db_set("orders_synced_until", synced_until)
				frappe.db.commit()
				return
			synced_until = max(
				synced_until,
				get_datetime(order["lastModifiedDate"]).replace(tzinfo=None) + timedelta(seconds=1),
			)
		if not page.get("next"):
			break
		offset += len(page.get("orders") or [])
	settings.db_set("orders_synced_until", synced_until)
	frappe.db.commit()


# ── schedule + doc events ────────────────────────────────────────────────────────────────────────


def run_all():
	for name in frappe.get_all("eBay Sync Settings", filters={"enabled": 1}, pluck="name"):
		settings = frappe.get_doc("eBay Sync Settings", name)
		for enabled, step in (
			(settings.create_listings, push_listings),
			(1, push_stock_price),
			(settings.import_orders, import_orders),
		):
			if not enabled:
				continue
			try:
				step(name)
			except EbayError, OSError, frappe.ValidationError:
				frappe.db.rollback()
				frappe.log_error(
					title=f"eBay sync {name}: {step.__name__} failed", message=frappe.get_traceback()
				)


def syncs_for_item(item_code):
	group = frappe.db.get_value("Item", item_code, "item_group")
	if not group:
		return []
	lft, rgt = frappe.db.get_value("Item Group", group, ["lft", "rgt"])
	out = []
	for s in frappe.get_all(
		"eBay Sync Settings", filters={"enabled": 1}, fields=["name", "item_group", "warehouse"]
	):
		g_lft, g_rgt = frappe.db.get_value("Item Group", s.item_group, ["lft", "rgt"])
		if g_lft <= lft and rgt <= g_rgt:
			out.append(s)
	return out


def on_price_change(doc, method=None):
	"""Item Price saved → push that item's price/quantity to every sync covering it."""
	for s in syncs_for_item(doc.item_code):
		frappe.enqueue(
			"ebay_sync.sync.push_stock_price",
			settings_name=s.name,
			item_codes=[doc.item_code],
			queue="short",
			enqueue_after_commit=True,
		)


def on_stock_change(doc, method=None):
	"""Stock Ledger Entry submitted/cancelled → push that item's quantity to syncs on its warehouse."""
	for s in syncs_for_item(doc.item_code):
		if s.warehouse == doc.warehouse:
			frappe.enqueue(
				"ebay_sync.sync.push_stock_price",
				settings_name=s.name,
				item_codes=[doc.item_code],
				queue="short",
				enqueue_after_commit=True,
			)
