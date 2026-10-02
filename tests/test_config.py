import tomllib
from pathlib import Path

import pytest

from autopara.config import ConfigError, load_config, parse_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def test_example_config_is_valid():
    config = load_config(str(EXAMPLE))
    assert config.schedule.group == "ТЦР-32"
    assert config.join.zoom_mode == "app"
    assert config.links[0].type == "Лк"
    assert config.links[-1].skip


@pytest.mark.parametrize(
    "toml, message",
    [
        ("[schedule]\n", "group"),
        ('[schedule]\ngroup = "ТЦР-32"\ngruop_id = 1\n', "невідомі параметри gruop_id"),
        ('[schedule]\ngroup = "ТЦР-32"\ncourse = "3"\n', "course"),
        ('[schedule]\ngroup = "ТЦР-32"\ncourse = true\n', "course"),
        ('[schedule]\ngroup = "ТЦР-32"\n[join]\nzoom_mode = "web"\n', "zoom_mode"),
        ('[schedule]\ngroup = "ТЦР-32"\n[[links]]\nsubject = "БД"\n', "url або skip"),
        ('[schedule]\ngroup = "ТЦР-32"\n[[links]]\nurl = "https://meet.google.com/a"\n', "subject"),
    ],
)
def test_config_errors(toml, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(tomllib.loads(toml))


def test_links_are_normalized():
    config = parse_config(
        tomllib.loads('[schedule]\ngroup = "ТЦР-32"\n[[links]]\nsubject = "БД"\nurl = " meet.google.com/aaa-bbbb-ccc "\n')
    )
    assert config.links[0].url == "https://meet.google.com/aaa-bbbb-ccc"
