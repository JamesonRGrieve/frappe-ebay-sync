# frappe-ebay-sync — Agent Operating Guide

AGPL-3.0-or-later Frappe v16 app syncing ERPNext ↔ eBay for any seller (one `eBay Sync Settings` per
eBay account ↔ ERPNext company; operator: Jameson Grieve first, Zephyrex/3S Hub later; eBay.ca).

## Model

- **ERPNext is master** for stock and prices, and for listings when `create_listings` is on (operator: optional).
- API shapes come from eBay's OpenAPI specs (sell_inventory_v1, sell_fulfillment_v1). OAuth uses the refresh-token grant; the access token is cached in Redis until shortly before expiry.
- Listings:
  - a stand-alone item → inventory item → offer → publish;
  - a variant template → one inventory item per variant (aspects from the variant attributes) + an inventory item group (`variesBy` from the attribute names) + an offer per variant → `publish_by_inventory_item_group`.
  - Offers and listings are tracked in `eBay Sync Link`.
- Stock/price: `bulk_update_price_quantity` (≤25 per call) with max(actual − reserved, 0) and the price-list rate.
- Orders:
  - getOrders `lastmodifieddate:[cursor..]` (≤200/page). Only PAID and not-cancelled orders are imported.
  - An order becomes a Sales Invoice (update_stock, SKU = item code, shipping as an Actual row; eBay-remitted tax excluded) plus a Payment Entry (eBay fees as a deduction).
  - Each buyer becomes a Customer, keyed by eBay username, plus a Shipping Address. Buyer email is an eBay relay, and PII is masked after 90 days.
  - The cursor only advances past cleanly imported orders.
- `mapping.py` is pure (unit-tested). `sync.py` does the DB + API work. `ebay_api.py` is the HTTP client (60 s timeout).

## Testing

`bench --site <site> run-tests --app ebay_sync`. Mapping unit tests plus engine tests on the real test DB (ERPNext fixtures).
Go-live gate: an eBay sandbox seller round-trip covering a listing created and published, stock/price push, and a paid order imported into invoice/payment/stock.
