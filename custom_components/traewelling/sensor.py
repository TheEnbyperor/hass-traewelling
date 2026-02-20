import logging
import datetime
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import aiohttp_client, device_registry
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from . import _signal_sync, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    manager = SensorManager(hass, entry, async_add_entities)
    await manager.async_sync()
    entry.async_on_unload(
        async_dispatcher_connect(hass, _signal_sync(entry.entry_id), manager.async_schedule_sync)
    )


class SensorManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
        self.hass = hass
        self.entry = entry
        self._async_add_entities = async_add_entities
        self._coordinator = Coordinator(hass, entry)
        self._entities = {}

    @callback
    def async_schedule_sync(self) -> None:
        self.hass.async_create_task(self.async_sync())

    def _desired_from_subentries(self) -> dict[str, dict]:
        desired: dict[str, dict] = {}
        for sub in self.entry.subentries.values():
            unique_id = f"{self.entry.entry_id}:{sub.subentry_id}:checked_in"
            desired[unique_id] = {
                "entry": self.entry,
                "subentry": sub
            }
        return desired

    async def async_sync(self) -> None:
        to_delete = []
        for id, entities in self._entities.items():
            if id not in self.entry.subentries:
                for entity in entities:
                    await entity.async_remove()
                to_delete.append(id)

        for id in to_delete:
            del self._entities[id]

        to_add = []
        for id, subentry in self.entry.subentries.items():
            if subentry.subentry_type != "account":
                continue
            if id not in self._entities:
                dr = device_registry.async_get(self.hass)
                device = dr.async_get_or_create(
                    config_entry_id=self.entry.entry_id,
                    config_subentry_id=subentry.subentry_id,
                    identifiers={
                        (DOMAIN, f"{self.entry.unique_id}:{subentry.unique_id}")
                    }
                )
                self._entities[id] = [
                    CheckedInEntity(self._coordinator, device, id),
                    TotalDistanceEntity(self._coordinator, device, id),
                    TotalDurationEntity(self._coordinator, device, id),
                    UnreadNotificationsEntity(self._coordinator, device, id),
                    CheckinMessageEntity(self._coordinator, device, id),
                    CheckinLikesEntity(self._coordinator, device, id),
                    CheckinVisibilityEntity(self._coordinator, device, id),
                    CheckinTravelTypeEntity(self._coordinator, device, id),
                    CheckinCategoryEntity(self._coordinator, device, id),
                    CheckinLineNameEntity(self._coordinator, device, id),
                    CheckinOperatorEntity(self._coordinator, device, id),
                    CheckinDistanceEntity(self._coordinator, device, id),
                    CheckinDurationEntity(self._coordinator, device, id),
                    CheckinDepartureTimeEntity(self._coordinator, device, id),
                    CheckinDepartureStationEntity(self._coordinator, device, id),
                    CheckinArrivalTimeEntity(self._coordinator, device, id),
                    CheckinArrivalStationEntity(self._coordinator, device, id),
                ]
                to_add.append(id)

        if to_add:
            await self._coordinator.async_refresh()
            for id in to_add:
                self._async_add_entities(self._entities[id])


class Coordinator(DataUpdateCoordinator):
    def __init__(self, hass, config_entry):
        super().__init__(
            hass,
            _LOGGER,
            name="Träwelling",
            config_entry=config_entry,
            update_interval=datetime.timedelta(seconds=60),
            always_update=True
        )

    async def _async_setup(self):
        pass

    async def _async_update_data(self):
        session = aiohttp_client.async_get_clientsession(self.hass)
        data = {}

        for id, subentry in self.config_entry.subentries.items():
            if subentry.subentry_type != "account":
                continue

            dr = device_registry.async_get(self.hass)
            device = dr.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                config_subentry_id=subentry.subentry_id,
                identifiers={
                    (DOMAIN, f"{self.config_entry.unique_id}:{subentry.unique_id}")
                }
            )

            now = datetime.datetime.utcnow()
            expires_at = datetime.datetime.fromisoformat(subentry.data["expires_at"])
            if expires_at <= now:
                resp = await session.post(
                    f"{self.config_entry.data['instance_url']}/oauth/token",
                    json={
                        "grant_type": "refresh_token",
                        "client_id": self.config_entry.data["oauth_client_id"],
                        "refresh_token": subentry.data["refresh_token"],
                    }
                )
                if not resp.ok:
                    self.logger.error(f"Failed to refresh access token, got response {resp.status}")
                    continue
                data = await resp.json()
                if data["token_type"] != "Bearer":
                    self.logger.error(f"Invalid token type")
                    continue

                access_token = data["access_token"]
                refresh_token = data["refresh_token"]
                expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=int(data["expires_in"]))
                self.hass.config_entries.async_update_subentry(self.config_entry, subentry, data={
                    "token": access_token,
                    "refresh_token": refresh_token,
                    "expires_at": expires_at.isoformat(),
                })
                continue
            else:
                access_token = subentry.data["token"]

            resp = await session.get(
                f"{self.config_entry.data['instance_url']}/api/v1/auth/user",
                headers={
                    "Authorization": f"Bearer {access_token}",
                }
            )
            if not resp.ok:
                self.logger.error(f"Failed to fetch user data from Träwelling, got response {resp.status}")
                continue

            user_data = (await resp.json())["data"]
            data[id] = {
                "user": user_data,
            }

            dr.async_update_device(
                device.id,
                configuration_url=f"{self.config_entry.data['instance_url']}/@{user_data['username']}",
                name=user_data["displayName"],
            )

            resp = await session.get(
                f"{self.config_entry.data['instance_url']}/api/v1/user/statuses/active",
                headers={
                    "Authorization": f"Bearer {access_token}",
                }
            )
            if not resp.ok:
                self.logger.error(f"Failed to fetch check-in data from Träwelling, got response {resp.status}")
            else:
                if resp.status == 204:
                    data[id]["active_check_in"] = None
                else:
                    data[id]["active_check_in"] = (await resp.json())["data"]

            resp = await session.get(
                f"{self.config_entry.data['instance_url']}/api/v1/notifications/unread/count",
                headers={
                    "Authorization": f"Bearer {access_token}",
                }
            )
            if not resp.ok:
                self.logger.error(
                    f"Failed to fetch unread notifcation count from Träwelling, got response {resp.status}")
            else:
                data[id]["unread_notifications"] = int((await resp.json())["data"])

        return data


class TraewellingEntity(CoordinatorEntity):
    _entity_type_id: str

    def __init__(self, coordinator, device: DeviceEntry, id):
        super().__init__(coordinator)
        self.id = id
        self.device_entry = device
        self._attr_unique_id = f"{device.id}:{self._entity_type_id}"

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.id not in self.coordinator.data:
            self._attr_available = False
            self.async_write_ha_state()
            return

        self._handle_update(self.coordinator.data[self.id])
        self.async_write_ha_state()


class CheckedInEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checked_in"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["checked_in", "not_checked_in"]
    _attr_has_entity_name = True
    _attr_name = "Checkin State"

    def _handle_update(self, data) -> None:
        self._attr_entity_picture = data["user"]["profilePicture"]

        if "active_check_in" not in data:
            self._attr_available = False
            return

        active_check_in = data["active_check_in"]
        self._attr_native_value = "checked_in" if active_check_in is not None else "not_checked_in"


class TotalDistanceEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "total_distance"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = "m"
    _attr_has_entity_name = True
    _attr_name = "Total Distance"

    def _handle_update(self, data) -> None:
        self._attr_native_value = data["user"]["totalDistance"]


class TotalDurationEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "total_duration"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "min"
    _attr_suggested_unit_of_measurement = "d"
    _attr_has_entity_name = True
    _attr_name = "Total Duration"

    def _handle_update(self, data) -> None:
        self._attr_native_value = data["user"]["totalDuration"]


class UnreadNotificationsEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "unread_notifications"
    _attr_has_entity_name = True
    _attr_name = "Unread Notifications"

    def _handle_update(self, data) -> None:
        self._attr_native_value = data["unread_notifications"]


class CheckinMessageEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checkin_message"
    _attr_has_entity_name = True
    _attr_name = "Current Checkin Message"

    def _handle_update(self, data) -> None:
        if "active_check_in" not in data:
            self._attr_available = False
            return

        if data["active_check_in"] is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = data["active_check_in"]["body"]


class CheckinTravelTypeEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checkin_travel_type"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["private", "business", "commute"]
    _attr_has_entity_name = True
    _attr_name = "Current Checkin Travel Type"

    def _handle_update(self, data) -> None:
        if "active_check_in" not in data:
            self._attr_available = False
            return

        if data["active_check_in"] is None:
            self._attr_native_value = None
        else:
            if data["active_check_in"]["business"] == 0:
                self._attr_native_value = "private"
            elif data["active_check_in"]["business"] == 1:
                self._attr_native_value = "business"
            elif data["active_check_in"]["business"] == 2:
                self._attr_native_value = "commute"


class CheckinVisibilityEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checkin_visibility"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["public", "unlisted", "followers", "private", "authenticated", "trusted"]
    _attr_has_entity_name = True
    _attr_name = "Current Checkin Visibility"

    def _handle_update(self, data) -> None:
        if "active_check_in" not in data:
            self._attr_available = False
            return

        if data["active_check_in"] is None:
            self._attr_native_value = None
        else:
            if data["active_check_in"]["visibility"] == 0:
                self._attr_native_value = "public"
            elif data["active_check_in"]["visibility"] == 1:
                self._attr_native_value = "unlisted"
            elif data["active_check_in"]["visibility"] == 2:
                self._attr_native_value = "followers"
            elif data["active_check_in"]["visibility"] == 3:
                self._attr_native_value = "private"
            elif data["active_check_in"]["visibility"] == 4:
                self._attr_native_value = "authenticated"
            elif data["active_check_in"]["visibility"] == 5:
                self._attr_native_value = "trusted"


class CheckinLikesEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checkin_likes"
    _attr_has_entity_name = True
    _attr_name = "Current Checkin Likes"

    def _handle_update(self, data) -> None:
        if "active_check_in" not in data:
            self._attr_available = False
            return

        if data["active_check_in"] is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = data["active_check_in"]["likes"]


class CheckinCategoryEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checkin_category"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["nationalExpress", "national", "regionalExp", "regional", "suburban", "bus", "ferry", "subway",
                     "tram", "taxi", "plane"]
    _attr_has_entity_name = True
    _attr_name = "Current Checkin Transport Category"

    def _handle_update(self, data) -> None:
        if "active_check_in" not in data:
            self._attr_available = False
            return

        if data["active_check_in"] is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = data["active_check_in"]["checkin"]["category"]


class CheckinLineNameEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checkin_line_name"
    _attr_has_entity_name = True
    _attr_name = "Current Checkin Line Name"

    def _handle_update(self, data) -> None:
        if "active_check_in" not in data:
            self._attr_available = False
            return

        if data["active_check_in"] is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = data["active_check_in"]["checkin"]["lineName"]


class CheckinOperatorEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checkin_operator"
    _attr_has_entity_name = True
    _attr_name = "Current Checkin Operator"

    def _handle_update(self, data) -> None:
        if "active_check_in" not in data:
            self._attr_available = False
            return

        if data["active_check_in"] is None or not data["active_check_in"]["checkin"]["operator"]:
            self._attr_native_value = None
        else:
            self._attr_native_value = data["active_check_in"]["checkin"]["operator"]["identifier"] \
                                      or str(data["active_check_in"]["checkin"]["operator"]["id"])


class CheckinDistanceEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checkin_distance"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = "m"
    _attr_suggested_unit_of_measurement = "km"
    _attr_has_entity_name = True
    _attr_name = "Current Checkin Distance"

    def _handle_update(self, data) -> None:
        if "active_check_in" not in data:
            self._attr_available = False
            return

        if data["active_check_in"] is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = data["active_check_in"]["checkin"]["distance"]


class CheckinDurationEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checkin_duration"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "min"
    _attr_has_entity_name = True
    _attr_name = "Current Checkin Duration"

    def _handle_update(self, data) -> None:
        if "active_check_in" not in data:
            self._attr_available = False
            return

        if data["active_check_in"] is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = data["active_check_in"]["checkin"]["duration"]


class CheckinDepartureTimeEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checkin_departure_time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_has_entity_name = True
    _attr_name = "Current Checkin Departure Time"

    def _handle_update(self, data) -> None:
        if "active_check_in" not in data:
            self._attr_available = False
            return

        if data["active_check_in"] is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = datetime.datetime.fromisoformat(
                data["active_check_in"]["checkin"]["origin"]["departure"]
            )


class CheckinDepartureStationEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checkin_departure_station"
    _attr_has_entity_name = True
    _attr_name = "Current Checkin Departure Station"

    def _handle_update(self, data) -> None:
        if "active_check_in" not in data:
            self._attr_available = False
            return

        if data["active_check_in"] is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = str(data["active_check_in"]["checkin"]["origin"]["id"])


class CheckinArrivalTimeEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checkin_arrival_time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_has_entity_name = True
    _attr_name = "Current Checkin Arrival Time"

    def _handle_update(self, data) -> None:
        if "active_check_in" not in data:
            self._attr_available = False
            return

        if data["active_check_in"] is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = datetime.datetime.fromisoformat(
                data["active_check_in"]["checkin"]["destination"]["arrival"]
            )


class CheckinArrivalStationEntity(TraewellingEntity, SensorEntity):
    _entity_type_id = "checkin_arrival_station"
    _attr_has_entity_name = True
    _attr_name = "Current Checkin Arrival Station"

    def _handle_update(self, data) -> None:
        if "active_check_in" not in data:
            self._attr_available = False
            return

        if data["active_check_in"] is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = str(data["active_check_in"]["checkin"]["destination"]["id"])
