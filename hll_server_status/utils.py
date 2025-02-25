import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import discord

from hll_server_status import constants
from hll_server_status.models import URL, AppStore, Config, Map
from hll_server_status.parsers import (
    parse_gamestate,
    parse_map_rotation,
    parse_server_name,
    parse_slots,
    parse_vip_slots_num,
    parse_vips_count,
)


def bootstrap(logger: logging.Logger, directories=constants.MANDATORY_DIRECTORIES):
    for directory in directories:
        try:
            Path(directory).mkdir(exist_ok=True)
        except FileNotFoundError:
            logger.error(f"Unable to create {directory}")
            sys.exit(1)


def guess_current_map_rotation_positions(
    rotation: list[Map], current_map: Map, next_map: Map
) -> list[int]:
    """Estimate the index(es) of the current map in the rotation based off current/next map"""
    # As of U13 a map can be in a rotation more than once, but the index isn't
    # provided by RCON so we have to try to guess where we are in the rotation

    # TODO: what about single map rotations
    # TODO: use previous map to better estimate

    # Between rounds
    if current_map.raw_name == constants.BETWEEN_MATCHES_MAP_NAME:
        return []

    raw_names = [map.raw_name for map in rotation]

    # the current map is only in once then we know exactly where we are
    if raw_names.count(current_map.raw_name) == 1:
        return [raw_names.index(current_map.raw_name)]

    # the current map is in more than once, we must estimate
    # if the next map is in only once then we know exactly where we are
    current_map_idxs = []
    for idx in [idx for idx, name in enumerate(raw_names) if name == next_map.raw_name]:

        # if raw_names.count(next_map.raw_name) == 1:
        # next_map_idx = raw_names.index(next_map.raw_name)
        # current_map_idx = None

        # have to account for wrapping from the end to the start
        # current map is the end of the rotation
        if idx == 0:
            current_map_idx = len(raw_names) - 1
        # Somewhere besides the end of the rotation
        else:
            current_map_idx = idx - 1

        current_map_idxs.append(current_map_idx)
        # return [current_map_idx]

    return current_map_idxs

    # the current map is in more than once
    # and the next map is in multiple times so we can't determine where we are
    # return [idx for idx, name in enumerate(raw_names) if name == current_map.raw_name]


def guess_next_map_rotation_positions(
    current_map_positions: list[int], rotation: list[Map]
) -> list[int]:
    """Estimate the index(es) of the next map in the rotation based off current/next map"""
    rotation_length = len(rotation)

    positions: list[int] = []
    for position in current_map_positions:
        # handle wrapping back to the start of the rotation
        if position == rotation_length - 1:
            positions.append(0)
        # otherwise the next map is immediately after the current map
        else:
            positions.append(position + 1)

    return positions


def get_map_picture_url(
    config: Config, map: Map, map_prefix=constants.MAP_PICTURES
) -> URL | None:
    """Build and validate a URL to the CRCON map image"""
    if map.raw_name == constants.BETWEEN_MATCHES_MAP_NAME:
        return None

    base_map_name, _ = map.raw_name.split("_", maxsplit=1)
    url = (
        config.api.base_server_url
        + map_prefix
        + constants.MAP_TO_PICTURE[base_map_name]
    )

    # This is valid even though pylance complains about it
    return URL(url=url)  # type: ignore


async def build_header(
    app_store: AppStore,
    config: Config,
    get_api_result: Callable,
) -> tuple[str | None, discord.Embed | None]:
    """Build up the Discord.Embed for the header message"""
    ENDPOINTS_TO_PARSERS = {
        "get_gamestate": parse_gamestate,
        "get_vip_slots_num": parse_vip_slots_num,
        "get_vips_count": parse_vips_count,
        "get_status": parse_server_name,
        "get_slots": parse_slots,
    }

    OPTIONS_TO_ENDPOINTS = {
        "reserved_vip_slots": "get_vip_slots_num",
        "current_vips": "get_vips_count",
    }

    # TODO: Add map vote info

    header_embed = discord.Embed()

    result = await get_api_result(app_store, config, endpoint="get_status")

    if result is None:
        raise ValueError("")

    server_name = parse_server_name(result)

    match config.display.header.server_name:
        case "name":
            header_embed.title = server_name.name
        case "short_name":
            header_embed.title = server_name.short_name

    if url := config.display.header.quick_connect_url:
        header_embed.add_field(
            name=config.display.header.quick_connect_name, value=url, inline=False
        )

    if url := config.display.header.battlemetrics_url:
        header_embed.add_field(
            name=config.display.header.battlemetrics_name, value=url, inline=False
        )

    if config.display.header.embeds:
        for option in config.display.header.embeds:
            endpoint = OPTIONS_TO_ENDPOINTS[option.value]
            result = await get_api_result(app_store, config, endpoint=endpoint)
            parser = ENDPOINTS_TO_PARSERS[endpoint]
            value = parser(result)
            header_embed.add_field(name=option.name, value=value, inline=option.inline)

    footer_text = ""
    if config.display.header.footer.enabled:
        footer_text = f"{config.display.header.footer.footer_text}{config.display.header.footer.last_refresh_text}"

    if config.display.header.footer.include_timestamp:
        if footer_text:
            header_embed.set_footer(text=footer_text)
        header_embed.timestamp = datetime.now()

    return None, header_embed


async def build_gamestate(
    app_store: AppStore,
    config: Config,
    get_api_result: Callable,
    endpoint: str = "get_gamestate",
) -> tuple[str | None, discord.Embed | None]:
    """Build up the Discord.Embed for the gamestate message"""
    gamestate_embed = discord.Embed()

    result: dict[str, Any] = await get_api_result(app_store, config, endpoint=endpoint)
    gamestate = parse_gamestate(app_store, result)

    if config.display.gamestate.image:
        url = get_map_picture_url(config, gamestate["current_map"])

        if url:
            gamestate_embed.set_image(url=url.url)

    for option in config.display.gamestate.embeds:
        if option.value == "slots":
            result = await get_api_result(app_store, config, endpoint="get_slots")
            slots = parse_slots(result)
            value = f"{slots.player_count}/{slots.max_players}"
        elif option.value == constants.EMPTY_EMBED:
            value = option.value
        elif option.value == "score":
            if (
                config.display.gamestate.score_format_ger_us
                and gamestate["current_map"].raw_name in constants.US_MAPS
            ):
                format_str = config.display.gamestate.score_format_ger_us
            elif (
                config.display.gamestate.score_format_ger_rus
                and gamestate["current_map"].raw_name in constants.RUSSIAN_MAPS
            ):
                format_str = config.display.gamestate.score_format_ger_rus
            else:
                format_str = config.display.gamestate.score_format

            value = format_str.format(
                gamestate["allied_score"], gamestate["axis_score"]
            )
        # make mypy happy by using string literals in the gamestate typed dict
        # instead of doing it dynamically
        elif option.value == "current_map":
            value = gamestate["current_map"].name
        elif option.value == "next_map":
            value = gamestate["next_map"].name
        elif option.value == "time_remaining":
            value = str(gamestate["time_remaining"])
        elif option.value == "num_allied_players":
            value = str(gamestate["num_allied_players"])
        elif option.value == "num_axis_players":
            value = str(gamestate["num_axis_players"])
        else:
            raise ValueError(
                f"Invalid {option.value} in [[display.gamestate.embeds]] for {app_store.server_identifier}"
            )

        gamestate_embed.add_field(name=option.name, value=value, inline=option.inline)

    if config.display.gamestate.footer.enabled:
        footer_text = f"{config.display.gamestate.footer.footer_text}{config.display.gamestate.footer.last_refresh_text}"

        if config.display.gamestate.footer.include_timestamp:
            if footer_text:
                gamestate_embed.set_footer(text=footer_text)
            gamestate_embed.timestamp = datetime.now()

    return None, gamestate_embed


async def build_map_rotation_color(
    app_store: AppStore,
    config: Config,
    get_api_result: Callable,
    endpoint: str = "get_map_rotation",
) -> tuple[str | None, discord.Embed | None]:
    """Build up the content str for the map rotation color message"""
    app_store.logger.error(app_store)
    app_store.logger.error(config)
    app_store.logger.exception("Should not be here")
    raise Exception
    result = await get_api_result(app_store, config, endpoint=endpoint)
    map_rotation = parse_map_rotation(result)

    gamestate_result = await get_api_result(app_store, config, endpoint="get_gamestate")

    gamestate = parse_gamestate(app_store, gamestate_result)
    current_map_positions = guess_current_map_rotation_positions(
        map_rotation, gamestate["current_map"], gamestate["next_map"]
    )

    next_map_positions = guess_next_map_rotation_positions(
        current_map_positions, map_rotation
    )

    app_store.logger.debug(f"current map positions color {current_map_positions=}")
    app_store.logger.debug(f"next map positions color {next_map_positions}")

    content: list[str] = []

    if config.display.map_rotation.color.display_title:
        content.append(config.display.map_rotation.color.title)

    start_block = "```"
    end_block = "```"
    current_map_color = constants.COLOR_TO_CODE_BLOCK[
        config.display.map_rotation.color.current_map_color
    ]
    next_map_color = constants.COLOR_TO_CODE_BLOCK[
        config.display.map_rotation.color.next_map_color
    ]
    other_map_color = constants.COLOR_TO_CODE_BLOCK[
        config.display.map_rotation.color.other_map_color
    ]

    for idx, map in enumerate(map_rotation):
        if idx in current_map_positions:
            style = current_map_color
        elif idx in next_map_positions:
            style = next_map_color
        # other map color
        else:
            style = other_map_color
        line = start_block + style + "\n" + map.name + "\n" + end_block
        content.append(line)

    if config.display.map_rotation.color.display_legend:
        content.append(config.display.map_rotation.color.legend_title)
        current, next, other = config.display.map_rotation.color.legend

        content.append(start_block + current_map_color + "\n" + current + end_block)
        content.append(start_block + next_map_color + "\n" + next + end_block)
        content.append(start_block + other_map_color + "\n" + other + end_block)

    if config.display.map_rotation.color.display_last_refreshed:
        content.append(
            config.display.map_rotation.color.last_refresh_text.format(
                int(datetime.now().timestamp())
            )
        )

    return "".join(content), None


async def build_map_rotation_embed(
    app_store: AppStore,
    config: Config,
    get_api_result: Callable,
    endpoint: str = "get_map_rotation",
) -> tuple[str | None, discord.Embed | None]:
    """Build up the Discord.Embed for the map rotation embed message"""
    result = await get_api_result(app_store, config, endpoint=endpoint)
    map_rotation = parse_map_rotation(result)

    gamestate_result = await get_api_result(app_store, config, endpoint="get_gamestate")
    gamestate = parse_gamestate(app_store, gamestate_result)

    current_map_positions = guess_current_map_rotation_positions(
        map_rotation, gamestate["current_map"], gamestate["next_map"]
    )
    next_map_positions = guess_next_map_rotation_positions(
        current_map_positions, map_rotation
    )

    app_store.logger.debug(f"current map positions embed {current_map_positions=}")
    app_store.logger.debug(f"next map positions embed {next_map_positions}")

    map_rotation_embed = discord.Embed()

    description = []
    for idx, map in enumerate(map_rotation):
        if idx in current_map_positions:
            description.append(
                config.display.map_rotation.embed.current_map.format(map.name, idx + 1)
            )
        elif idx in next_map_positions:
            description.append(
                config.display.map_rotation.embed.next_map.format(map.name, idx + 1)
            )
        # other map
        else:
            description.append(
                config.display.map_rotation.embed.other_map.format(map.name, idx + 1)
            )

    if config.display.map_rotation.embed.display_legend:
        description.append(config.display.map_rotation.embed.legend)

    map_rotation_embed.add_field(
        name=config.display.map_rotation.embed.title, value="\n".join(description)
    )
    
    if config.display.map_rotation.embed.bm_banner_enabled:
        timestamp = int(time.time()*1000)
        url = config.display.map_rotation.embed.bm_banner_url + "?id=" + str(timestamp)
        map_rotation_embed.set_image(url=url)

    footer_text = ""
    if config.display.map_rotation.embed.footer.enabled:
        footer_text = f"{config.display.map_rotation.embed.footer.footer_text}{config.display.map_rotation.embed.footer.last_refresh_text}"

        if config.display.map_rotation.embed.footer.include_timestamp:
            if footer_text:
                map_rotation_embed.set_footer(text=footer_text)
            map_rotation_embed.timestamp = datetime.now()

    return None, map_rotation_embed
