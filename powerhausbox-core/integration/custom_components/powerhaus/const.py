"""Constants for the PowerHaus integration."""

DOMAIN = "powerhaus"
INTEGRATION_NAME = "PowerHaus Backup"

# Add-on port (nginx on the add-on)
ADDON_PORT = 8099

# Health check path on the add-on
ADDON_HEALTH_PATH = "/_powerhausbox/api/healthz"

# Chunk size for streaming backup data (256 KB)
BACKUP_STREAM_CHUNK_SIZE = 262144
