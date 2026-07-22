"""The VW Group EU Data Act integration."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EudaApiClient
from .brands import DEFAULT_BRAND, get_brand
from .const import (
    CONF_BRAND,
    CONF_EMAIL,
    CONF_LAST_CONNECTED_OFFSET_HOURS,
    CONF_PASSWORD,
    CONF_VIN,
    DEFAULT_LAST_CONNECTED_OFFSET_HOURS,
    DOMAIN,
    raw_unique_id,
)
from .coordinator import EudaCoordinator
from .data import last_connected_time, load_dictionary
from .entity_migration import (
    async_migrate_entity_translations,
    async_sync_distance_registry_units,
)
from .issues import async_clear_issues, async_update_issues
from .services import async_setup_services, async_unload_services
from .utility_meter import async_ensure_utility_meters

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]


def _last_connected_offset_hours(entity) -> float:
    """Return a validated per-config-entry offset in hours."""
    value = entity.coordinator.entry.options.get(
        CONF_LAST_CONNECTED_OFFSET_HOURS,
        DEFAULT_LAST_CONNECTED_OFFSET_HOURS,
    )
    try:
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_LAST_CONNECTED_OFFSET_HOURS


def _last_connected_native_value(entity):
    """Return Last connected with the optional manual offset applied."""
    raw_timestamp = last_connected_time(entity.coordinator.data or {})
    if raw_timestamp is None:
        return entity._sticky_monotonic(None)

    return entity._sticky_monotonic(
        raw_timestamp + timedelta(hours=_last_connected_offset_hours(entity))
    )


def _last_connected_extra_state_attributes(entity) -> dict:
    """Expose the raw timestamp and configured workaround for diagnostics."""
    raw_timestamp = last_connected_time(entity.coordinator.data or {})
    offset = _last_connected_offset_hours(entity)
    return {
        "raw_timestamp": (
            raw_timestamp.isoformat() if raw_timestamp is not None else None
        ),
        "manual_offset_hours": offset,
        "manual_offset_applied": offset != 0,
    }


def _patch_last_connected_sensor() -> None:
    """Replace the fork's fixed offset with a configurable entity property."""
    from . import sensor as sensor_platform

    sensor_platform.EudaLastConnectedSensor.timestamp_offset_hours = property(
        _last_connected_offset_hours
    )
    sensor_platform.EudaLastConnectedSensor.native_value = property(
        _last_connected_native_value
    )
    sensor_platform.EudaLastConnectedSensor.extra_state_attributes = property(
        _last_connected_extra_state_attributes
    )


@callback
def _async_options_updated(hass: HomeAssistant, entry: "EudaConfigEntry") -> None:
    """Refresh entity states after an option changes, without reloading."""
    if entry.runtime_data:
        entry.runtime_data.coordinator.async_update_listeners()


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration (services)."""
    async_setup_services(hass)
    return True


async def async_unload(hass: HomeAssistant) -> bool:
    """Unload the integration."""
    async_unload_services(hass)
    return True


@dataclass
class EudaRuntimeData:
    coordinator: EudaCoordinator
    session: object


type EudaConfigEntry = ConfigEntry[EudaRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: EudaConfigEntry) -> bool:
    """Set up VW Group EU Data Act from a config entry.

    Setup is intentionally non-blocking: the first portal dataset can take
    15–60 minutes to appear after subscription, so blocking setup on it would
    leave the user staring at "Setting up..." for a long time. Instead the
    entry loads immediately with a single status sensor explaining what the
    integration is waiting for; the dataset-derived entities appear via the
    discovery listener as soon as real data arrives.
    """
    session = aiohttp.ClientSession(
        connector=async_get_clientsession(hass).connector,
        connector_owner=False,
        cookie_jar=aiohttp.CookieJar(),
    )
    try:
        await hass.async_add_executor_job(load_dictionary)

        brand = get_brand(entry.data.get(CONF_BRAND, DEFAULT_BRAND))
        client = EudaApiClient(
            session,
            entry.data[CONF_EMAIL],
            entry.data[CONF_PASSWORD],
            brand,
        )
        coordinator = EudaCoordinator(hass, entry, client)
        entry.runtime_data = EudaRuntimeData(coordinator=coordinator, session=session)
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))

        _patch_last_connected_sensor()

        await _async_migrate_raw_unique_ids(hass, entry)
        await async_migrate_entity_translations(hass, entry)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        if cached_points := await coordinator.async_restore_from_cache():
            coordinator.status_label = "ok"
            coordinator.async_set_updated_data(cached_points)

        @callback
        def _on_coordinator_update() -> None:
            async_update_issues(hass, entry, coordinator)
            async_sync_distance_registry_units(hass, entry, coordinator.data or {})
            hass.async_create_task(async_ensure_utility_meters(hass, entry))

        entry.async_on_unload(coordinator.async_add_listener(_on_coordinator_update))
        _on_coordinator_update()

        entry.async_create_background_task(
            hass,
            coordinator.async_refresh(),
            name=f"{DOMAIN} initial refresh {entry.data[CONF_VIN]}",
        )
    except Exception:
        await session.close()
        raise

    return True


async def _async_migrate_raw_unique_ids(hass: HomeAssistant, entry: EudaConfigEntry) -> None:
    """Prefix legacy raw-sensor unique_ids (bare dataset key -> VIN_key)."""
    vin = entry.data[CONF_VIN]
    prefix = f"{vin}_"

    @callback
    def _migrate(reg_entry: er.RegistryEntry) -> dict | None:
        if reg_entry.domain != "sensor":
            return None
        if reg_entry.unique_id.startswith(prefix):
            return None
        return {"new_unique_id": raw_unique_id(vin, reg_entry.unique_id)}

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)


async def async_unload_entry(hass: HomeAssistant, entry: EudaConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        async_clear_issues(hass, entry)
        if entry.runtime_data:
            await entry.runtime_data.session.close()
    return unload_ok
