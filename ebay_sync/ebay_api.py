# SPDX-License-Identifier: AGPL-3.0-or-later
"""The eBay Sell API calls this app makes: OAuth (refresh-token grant), Inventory API (inventory items,
item groups, offers, publish, bulk price/quantity) and Fulfillment API (getOrders). Shapes follow
eBay's published OpenAPI specs (sell_inventory_v1, sell_fulfillment_v1)."""

import base64
from urllib.parse import quote

import requests

HOSTS = {"Sandbox": "https://api.sandbox.ebay.com", "Production": "https://api.ebay.com"}
SCOPES = " ".join(
	[
		"https://api.ebay.com/oauth/api_scope/sell.inventory",
		"https://api.ebay.com/oauth/api_scope/sell.fulfillment",
		"https://api.ebay.com/oauth/api_scope/sell.account.readonly",
	]
)
REQUEST_TIMEOUT_SECONDS = 60
PRICE_QUANTITY_MAX = 25  # requests per bulk_update_price_quantity call
ORDERS_PAGE_LIMIT = 200  # getOrders maximum
TOKEN_REFRESH_MARGIN_SECONDS = 300
CONTENT_LANGUAGE = {"EBAY_CA": "en-CA", "EBAY_US": "en-US"}


class EbayError(Exception):
	"""An eBay API call failed; ``detail`` is eBay's error payload (safe to log)."""

	def __init__(self, message, detail=None):
		super().__init__(message)
		self.detail = detail


def token_request(client_id, client_secret, refresh_token):
	"""Headers + form body for eBay's refresh-token grant (access tokens last ~2 hours)."""
	basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
	return (
		{"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
		{"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": SCOPES},
	)


class Client:
	def __init__(self, environment, marketplace_id, access_token):
		self.host = HOSTS[environment]
		self.marketplace_id = marketplace_id
		self.access_token = access_token

	@staticmethod
	def refresh_access_token(environment, client_id, client_secret, refresh_token):
		"""(access token, expires_in seconds) from a seller's refresh token."""
		headers, form = token_request(client_id, client_secret, refresh_token)
		response = requests.post(
			HOSTS[environment] + "/identity/v1/oauth2/token",
			data=form,
			headers=headers,
			timeout=REQUEST_TIMEOUT_SECONDS,
		)
		data = response.json() if response.content else {}
		if response.status_code >= 400 or "access_token" not in data:
			raise EbayError(f"eBay token refresh failed with HTTP {response.status_code}", data)
		return data["access_token"], int(data.get("expires_in") or 0)

	def _call(self, method, path, body=None, params=None):
		headers = {
			"Authorization": f"Bearer {self.access_token}",
			"Accept": "application/json",
			"Content-Type": "application/json",
			"Content-Language": CONTENT_LANGUAGE.get(self.marketplace_id, "en-US"),
		}
		response = requests.request(
			method,
			self.host + path,
			json=body,
			params=params,
			headers=headers,
			timeout=REQUEST_TIMEOUT_SECONDS,
		)
		data = response.json() if response.content else {}
		if response.status_code >= 400:
			raise EbayError(f"eBay {method} {path} failed with HTTP {response.status_code}", data)
		return data

	def put_inventory_item(self, sku, body):
		return self._call("PUT", f"/sell/inventory/v1/inventory_item/{quote(sku, safe='')}", body)

	def put_inventory_item_group(self, key, body):
		return self._call("PUT", f"/sell/inventory/v1/inventory_item_group/{quote(key, safe='')}", body)

	def get_offers(self, sku):
		return (
			self._call(
				"GET", "/sell/inventory/v1/offer", params={"sku": sku, "marketplace_id": self.marketplace_id}
			).get("offers")
			or []
		)

	def create_offer(self, body):
		return self._call("POST", "/sell/inventory/v1/offer", body)["offerId"]

	def publish_offer(self, offer_id):
		return self._call("POST", f"/sell/inventory/v1/offer/{offer_id}/publish")["listingId"]

	def publish_item_group(self, key):
		return self._call(
			"POST",
			"/sell/inventory/v1/offer/publish_by_inventory_item_group",
			{"inventoryItemGroupKey": key, "marketplaceId": self.marketplace_id},
		)["listingId"]

	def bulk_update_price_quantity(self, requests_):
		return (
			self._call("POST", "/sell/inventory/v1/bulk_update_price_quantity", {"requests": requests_}).get(
				"responses"
			)
			or []
		)

	def get_orders(self, modified_since, offset=0):
		return self._call(
			"GET",
			"/sell/fulfillment/v1/order",
			params={
				"filter": f"lastmodifieddate:[{modified_since}..]",
				"limit": ORDERS_PAGE_LIMIT,
				"offset": offset,
			},
		)
