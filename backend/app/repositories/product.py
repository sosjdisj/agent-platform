"""产品 Repository。"""
from app.models.product import Product

from app.repositories.base import BaseRepository


class ProductRepository(BaseRepository[Product]):
    model = Product
