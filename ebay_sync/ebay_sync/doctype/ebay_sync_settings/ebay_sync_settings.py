# SPDX-License-Identifier: AGPL-3.0-or-later
import frappe
from frappe import _
from frappe.model.document import Document


class eBaySyncSettings(Document):
	def validate(self):
		if self.warehouse and frappe.db.get_value("Warehouse", self.warehouse, "company") != self.company:
			frappe.throw(_("Warehouse {0} does not belong to {1}.").format(self.warehouse, self.company))
		for field in ("deposit_account", "fee_account", "shipping_account", "tax_account"):
			account = self.get(field)
			if account and frappe.db.get_value("Account", account, "company") != self.company:
				frappe.throw(
					_("{0} {1} does not belong to {2}.").format(
						self.meta.get_label(field), account, self.company
					)
				)
