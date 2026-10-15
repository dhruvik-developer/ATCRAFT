"""Dealer portal: catalogue, cart, wishlist and checkout for a dealer account.

The module sits downstream of sales. It owns the offer, cart and wishlist
tables, and it places orders by calling ``sales.service.create_order`` with
prices and discounts the server computed. Staff administration of offers is
WP C3b; the screens are WP C4.
"""
