"""Sensor platform for Tesla / Autel BLE TPMS."""
from __future__ import annotations

from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfPressure,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.sensor import sensor_device_info_to_hass_device_info
from sensor_state_data import SensorUpdate

from .coordinator import TeslaTpmsConfigEntry
from .device import device_key_to_bluetooth_entity_key
from .entity import TeslaTPMSEntity
from .parser import TPMSSensor

# Everything arrives by advertisement or over one held connection, so there is
# nothing for Home Assistant to rate-limit.
PARALLEL_UPDATES = 0

# Entities with a device class are named from it -- "Pressure", "Battery" --
# and that name is already translated, so they need no translation_key of
# their own. The rest do, and theirs live in strings.json.
SENSOR_DESCRIPTIONS: dict[str, SensorEntityDescription] = {
    TPMSSensor.PRESSURE: SensorEntityDescription(
        key=TPMSSensor.PRESSURE,
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPressure.BAR,
        # One decimal, not two. The wire quantum is 1 kPa (0.01 bar) but Autel
        # specs +-0.1 bar for this part, so a second decimal is noise dressed
        # up as precision -- two wheels at the same pressure read 0.02 apart.
        # The unrounded value is still what gets stored and compared against.
        suggested_display_precision=1,
    ),
    TPMSSensor.TEMPERATURE: SensorEntityDescription(
        key=TPMSSensor.TEMPERATURE,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    TPMSSensor.BATTERY: SensorEntityDescription(
        key=TPMSSensor.BATTERY,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
    ),
    TPMSSensor.VOLTAGE: SensorEntityDescription(
        key=TPMSSensor.VOLTAGE,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
    ),
    TPMSSensor.SIGNAL_STRENGTH: SensorEntityDescription(
        key=TPMSSensor.SIGNAL_STRENGTH,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    # Diagnostics. These are the untouched wire values; they exist so the
    # decode can be checked against a reference gauge. See docs/CALIBRATION.md.
    TPMSSensor.RAW_PRESSURE: SensorEntityDescription(
        key=TPMSSensor.RAW_PRESSURE,
        translation_key=TPMSSensor.RAW_PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    TPMSSensor.RAW_TEMPERATURE: SensorEntityDescription(
        key=TPMSSensor.RAW_TEMPERATURE,
        translation_key=TPMSSensor.RAW_TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    TPMSSensor.STATUS: SensorEntityDescription(
        key=TPMSSensor.STATUS,
        translation_key=TPMSSensor.STATUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
}


def sensor_update_to_bluetooth_data_update(
    sensor_update: SensorUpdate,
) -> PassiveBluetoothDataUpdate:
    """Convert a sensor update to a Bluetooth data update."""
    return PassiveBluetoothDataUpdate(
        devices={
            device_id: sensor_device_info_to_hass_device_info(device_info)
            for device_id, device_info in sensor_update.devices.items()
        },
        entity_descriptions={
            device_key_to_bluetooth_entity_key(device_key): SENSOR_DESCRIPTIONS[
                device_key.key
            ]
            for device_key in sensor_update.entity_descriptions
            if device_key.key in SENSOR_DESCRIPTIONS
        },
        entity_data={
            device_key_to_bluetooth_entity_key(device_key): sensor_values.native_value
            for device_key, sensor_values in sensor_update.entity_values.items()
        },
        # Deliberately nameless: a name here would win over the description,
        # and the parser's are hardcoded English. None lets the device class or
        # the translation key name the entity instead.
        entity_names={
            device_key_to_bluetooth_entity_key(device_key): None
            for device_key in sensor_update.entity_values
        },
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaTpmsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the TPMS sensors."""
    coordinator = entry.runtime_data.coordinator
    processor = PassiveBluetoothDataProcessor(sensor_update_to_bluetooth_data_update)
    entry.async_on_unload(
        processor.async_add_entities_listener(
            TeslaTPMSSensorEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        coordinator.async_register_processor(processor, SensorEntityDescription)
    )


class TeslaTPMSSensorEntity(TeslaTPMSEntity, SensorEntity):
    """A single value from a Tesla-protocol TPMS sensor."""

    @property
    def native_value(self) -> str | int | float | None:
        """Return the native value."""
        return self.processor.entity_data.get(self.entity_key)
