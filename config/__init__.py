import sys
from .celery import app as celery_app

# Патч совместимости Django 5.1 с Python 3.14+
# В Python 3.14 copy(super()) возвращает прокси super, а не копию инстанса,
# что приводило к AttributeError: 'super' object has no attribute 'dicts'
if sys.version_info >= (3, 14):
    try:
        from django.template.context import BaseContext

        def _fixed_basecontext_copy(self):
            cls = self.__class__
            duplicate = cls.__new__(cls)
            duplicate.__dict__.update(self.__dict__)
            duplicate.dicts = self.dicts[:]
            return duplicate

        BaseContext.__copy__ = _fixed_basecontext_copy
    except ImportError:
        pass

__all__ = ("celery_app",)
