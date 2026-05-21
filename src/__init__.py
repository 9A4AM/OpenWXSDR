"""
OpenWXSDR - Streamlined Radiosonde Decoder Framework
"""

__version__ = '1.0.45'
__software_name__ = 'OpenWXSDR'
__author__ = 'OpenWX Team'

__all__ = ['OpenWXSDR']


def __getattr__(name):
	"""Lazy export to avoid import-time cycles during package init."""
	if name == 'OpenWXSDR':
		from .openwxsdr_app import OpenWXSDR
		return OpenWXSDR
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
