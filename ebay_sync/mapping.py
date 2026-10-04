# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure ERPNext ↔ eBay shape mapping (no DB, no network), unit-tested: inventory items, item groups
(variations), offers, price/quantity updates, paid orders → Sales Invoice values, buyers → Customer
and shipping address values. eBay amounts are decimal strings in Amount {"value", "currency"}."""

from decimal import Decimal

TITLE_MAX = 80  # eBay listing title limit
DESCRIPTION_MAX = 500000
PAYABLE_STATUSES = ("PAID",)


def amount(value):
	"""eBay Amount {"value": "12.34", ...} → Decimal (missing → 0)."""
	return Decimal(str((value or {}).get("value") or 0))


def available_quantity(actual_qty, reserved_qty):
	"""What eBay may sell: ERPNext stock not already promised to open orders, never negative."""
	return max(Decimal(str(actual_qty or 0)) - Decimal(str(reserved_qty or 0)), Decimal(0))


def money(value, currency):
	return {"value": format(Decimal(str(value)).quantize(Decimal("0.01")), "f"), "currency": currency.upper()}


def inventory_item(item, quantity, image_urls, condition, aspects=None):
	"""InventoryItem for one sellable SKU (a stand-alone item or a variant)."""
	product = {
		"title": item["item_name"][:TITLE_MAX],
		"description": (item.get("description") or item["item_name"])[:DESCRIPTION_MAX],
		"imageUrls": image_urls,
	}
	if aspects:
		product["aspects"] = aspects
	return {
		"condition": condition,
		"product": product,
		"availability": {"shipToLocationAvailability": {"quantity": int(quantity)}},
	}


def variant_aspects(attributes):
	"""ERPNext variant attributes [(name, value)] → eBay aspects {name: [value]}."""
	return {name: [value] for name, value in attributes}


def item_group(template, variants, image_urls):
	"""InventoryItemGroup for an ERPNext variant template; ``variants`` = [{"item_code", "attributes":
	[(name, value)]}]. eBay lists one multi-variation listing whose variations vary by the attribute names."""
	values = {}
	for v in variants:
		for name, value in v["attributes"]:
			values.setdefault(name, [])
			if value not in values[name]:
				values[name].append(value)
	return {
		"title": template["item_name"][:TITLE_MAX],
		"description": (template.get("description") or template["item_name"])[:DESCRIPTION_MAX],
		"imageUrls": image_urls,
		"variantSKUs": [v["item_code"] for v in variants],
		"variesBy": {"specifications": [{"name": n, "values": vs} for n, vs in values.items()]},
	}


def offer(sku, settings, price, currency, quantity):
	"""A fixed-price offer for ``sku`` on the settings' marketplace (unpublished until published)."""
	return {
		"sku": sku,
		"marketplaceId": settings["marketplace_id"],
		"format": "FIXED_PRICE",
		"availableQuantity": int(quantity),
		"categoryId": settings["category_id"],
		"merchantLocationKey": settings["merchant_location_key"],
		"listingPolicies": {
			"fulfillmentPolicyId": settings["fulfillment_policy_id"],
			"paymentPolicyId": settings["payment_policy_id"],
			"returnPolicyId": settings["return_policy_id"],
		},
		"pricingSummary": {"price": money(price, currency)},
	}


def price_quantity(sku, quantity, offer_id=None, price=None, currency=None):
	"""One bulk_update_price_quantity request: ERPNext's quantity (and price) for ``sku``."""
	request = {"sku": sku, "shipToLocationAvailability": {"quantity": int(quantity)}}
	if offer_id:
		offer_row = {"offerId": offer_id, "availableQuantity": int(quantity)}
		if price is not None:
			offer_row["price"] = money(price, currency)
		request["offers"] = [offer_row]
	return request


def is_importable(order):
	"""A paid, not-cancelled order."""
	cancelled = ((order.get("cancelStatus") or {}).get("cancelState") or "NONE_REQUESTED") not in (
		"NONE_REQUESTED",
		"",
	)
	return order.get("orderPaymentStatus") in PAYABLE_STATUSES and not cancelled


def invoice_lines(order):
	"""Sales Invoice item rows (SKU = ERPNext item code). Raises KeyError on a line without a SKU (a
	listing made outside ERPNext), so the order is held back for a person to map."""
	rows = []
	for line in order.get("lineItems") or []:
		sku = line.get("sku")
		if not sku:
			raise KeyError(f"eBay line {line.get('lineItemId')} has no SKU")
		qty = Decimal(str(line.get("quantity") or 1))
		rows.append(
			{
				"item_code": sku,
				"qty": float(qty),
				"rate": float(amount(line.get("lineItemCost")) / qty),
				"description": line.get("title") or sku,
			}
		)
	return rows


def invoice_totals(order):
	"""Order-level amounts. Tax eBay collects and remits itself is not the seller's revenue."""
	summary = order.get("pricingSummary") or {}
	remitted = bool(order.get("ebayCollectAndRemitTax"))
	return {
		"discount": amount(summary.get("priceDiscount")).copy_abs(),
		"shipping": amount(summary.get("deliveryCost")) - amount(summary.get("deliveryDiscount")).copy_abs(),
		"tax": Decimal(0) if remitted else amount(summary.get("tax")),
		"fee": amount(order.get("totalMarketplaceFee")),
		"currency": (summary.get("total") or {}).get("currency") or "CAD",
	}


def buyer_values(order):
	buyer = order.get("buyer") or {}
	contact = buyer.get("buyerRegistrationAddress") or {}
	name = contact.get("fullName") or buyer.get("username")
	if not name:
		return None
	phone = (contact.get("primaryPhone") or {}).get("phoneNumber")
	return {
		"customer_name": name,
		"customer_type": "Company" if contact.get("companyName") else "Individual",
		"email_id": contact.get("email"),
		"mobile_no": phone,
	}


def shipping_address(order):
	"""ERPNext Address fields from the order's ship-to; None without street + city."""
	steps = order.get("fulfillmentStartInstructions") or []
	ship_to = ((steps[0] if steps else {}).get("shippingStep") or {}).get("shipTo") or {}
	addr = ship_to.get("contactAddress") or {}
	if not (addr.get("addressLine1") and addr.get("city")):
		return None
	return {
		"address_title": ship_to.get("fullName"),
		"address_line1": addr["addressLine1"],
		"address_line2": addr.get("addressLine2"),
		"city": addr["city"],
		"state": addr.get("stateOrProvince"),
		"pincode": addr.get("postalCode"),
		"country_code": addr.get("countryCode"),
	}
