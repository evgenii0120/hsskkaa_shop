from aiogram import Router
from . import panel, products, tickets, delivery

router = Router()
router.include_routers(
    panel.entry, 
    panel.router, 
    products.router, 
    tickets.router,
    delivery.router
)