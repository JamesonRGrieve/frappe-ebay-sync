# frappe-ebay-sync

Keeps ERPNext and eBay in step, one **eBay Sync Settings** per eBay seller account ↔ ERPNext company
(multi-seller; eBay.ca by default). ERPNext is the master.

| Data | Direction | How |
|---|---|---|
| Listings (optional: "Create Listings") | ERPNext → eBay | Inventory items. Variant templates become inventory item groups (multi-variation listings), then offers, then publish. SKU = item code. |
| Stock + prices | ERPNext → eBay | `bulk_update_price_quantity`, with available = actual − reserved in the sync warehouse, and prices from the sync's price list. |
| Paid orders | eBay → ERPNext | Submitted Sales Invoice (stock out, shipping as a charge row), then a Payment Entry with eBay fees as a deduction. Tax that eBay collects and remits is left out. |
| Buyers | eBay → ERPNext | Customer per eBay username, plus shipping addresses. |

Price and stock changes push on ERPNext doc events. Orders are polled every 15 minutes, which also
reconciles listings and stock. Every eBay object is tracked in **eBay Sync Link**, so re-runs never
duplicate. An order with a line that has no SKU, i.e. a listing made outside ERPNext, is held back
and retried.

Credentials (App ID, Cert ID, seller refresh token) are written by the deploy pipeline from
OpenBao; never enter them by hand or commit them.

License: AGPL-3.0-or-later.
