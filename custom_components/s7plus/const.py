"""Constants for the S7 Plus integration."""

from typing import Final

DOMAIN: Final = "s7plus"

# --- Connection (config entry data) -----------------------------------------
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_CONNECTION_TYPE: Final = "connection_type"
CONF_USE_TLS: Final = "use_tls"
CONF_TLS_CA: Final = "tls_ca"
CONF_TLS_CERT: Final = "tls_cert"
CONF_TLS_KEY: Final = "tls_key"
CONF_PASSWORD: Final = "password"

DEFAULT_PORT: Final = 102
CONNECTION_TYPES: Final = ("hmi", "es", "pg")

# --- Options -----------------------------------------------------------------
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_FAST_SCAN_INTERVAL: Final = "fast_scan_interval"
CONF_TIMEOUT: Final = "timeout"
CONF_LINK_TIMEOUT: Final = "link_timeout"
CONF_TAGS_PER_REQUEST: Final = "tags_per_request"
CONF_SOURCE_ENTRY: Final = "source_entry"

DEFAULT_SCAN_INTERVAL: Final = 10.0
DEFAULT_FAST_SCAN_INTERVAL: Final = 1.0
DEFAULT_TIMEOUT: Final = 5.0
DEFAULT_LINK_TIMEOUT: Final = 30.0
DEFAULT_TAGS_PER_REQUEST: Final = 20

# --- Tags (config subentries) -----------------------------------------------
SUBENTRY_TAG: Final = "tag"

CONF_PLATFORM: Final = "platform"
CONF_SOURCE: Final = "source"
SOURCE_SYMBOLIC: Final = "symbolic"
SOURCE_ABSOLUTE: Final = "absolute"

# Symbolic tag: the browse record of the tag, kept so no browse is needed at start.
CONF_TAG: Final = "tag"
# Absolute address in a non-optimized data block.
CONF_DB: Final = "db"
CONF_BYTE: Final = "byte"
CONF_BIT: Final = "bit"
CONF_TYPE: Final = "type"
CONF_LENGTH: Final = "length"

CONF_SCALE: Final = "scale"
CONF_VALUE_OFFSET: Final = "value_offset"
CONF_PRECISION: Final = "precision"
CONF_INVERT: Final = "invert"
CONF_ON_VALUE: Final = "on_value"
CONF_OFF_VALUE: Final = "off_value"
CONF_MIN: Final = "min"
CONF_MAX: Final = "max"
CONF_STEP: Final = "step"
CONF_OPTIONS: Final = "options"
CONF_FAST_POLL: Final = "fast_poll"
CONF_TEST_READ: Final = "test_read"
CONF_BLOCK: Final = "block"
CONF_FILTER: Final = "filter"
CONF_TAG_NAME: Final = "tag_name"

PLATFORMS_ORDER: Final = ("sensor", "binary_sensor", "switch", "number", "select")
