# SPDX-License-Identifier: AGPL-3.0-or-later
from . import __version__ as _version

app_name = "ebay_sync"
app_title = "eBay Sync"
app_publisher = "Zephyrex Technologies Limited"
app_description = "Keep ERPNext and eBay in step: listings, prices, stock, orders and buyers"
app_email = "jameson@zephyrex.ca"
app_license = "AGPL-3.0-or-later"
app_version = _version

required_apps = ["frappe", "erpnext"]

# ERPNext is master: price edits and stock movements push to eBay as they happen.
doc_events = {
	"Item Price": {"on_update": "ebay_sync.sync.on_price_change"},
	"Stock Ledger Entry": {
		"on_submit": "ebay_sync.sync.on_stock_change",
		"on_cancel": "ebay_sync.sync.on_stock_change",
	},
}

# Orders are polled (and listings/stock reconciled) on a schedule.
scheduler_events = {"cron": {"*/15 * * * *": ["ebay_sync.sync.run_all"]}}
