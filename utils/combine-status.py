#!/usr/bin/python3

import re
import sys
import traceback
from pathlib import Path
from argparse import ArgumentParser

import yaml
from jinja2 import Template

HTML_TEMPLATE = """
<html>
  <head>
    <title>Packages status</title>
<style>
body { font-family: sans-serif; }
td.current { background-color: green; }
td.current-testing { background-color: yellow; }
td.templates-itl-testing { background-color: yellow; }
td.templates-community-testing { background-color: yellow; }
td.unstable { background-color: blue; }
td.unreleased { background-color: red; }
td.built-unreleased { background-color: white; }
td.templates-itl { background-color: green; }
td.templates-community { background-color: green; }
td.days-testing { background-color: yellow; }
td.days-stable { background-color: green; }
td.no-version { background-color: red; font-weight: bold; }
td, th { border: solid 2px #dcdcdc; padding: 4px; }
table { border-collapse: collapse; }
</style>
  </head>
  <body>
{%- for release in status.keys() -%}
    <h1>Release {{release}}</h1>

{%- for distribution in status[release].get("component", {}).keys() %}
      <h2>Packages for <span class="dist">{{distribution}}</span></h2>
        <table><tr><th>Component</th><th>Version</th><th>Status</th></tr>
{%- for component, component_status in status[release]["component"][distribution].items() -%}
{%- if component_status["status"] != "no packages defined" -%}
          <tr>
            <td>{{component}}</td><td>{{component_status["tag"]}}</td>
{%- for repo in component_status["repo"] %}
            <td class="{{color(repo["name"], repo["min-age-days"])}}">{{repo["name"]}}</td>
            <td class="{{color(repo["days"], repo["min-age-days"])}}">{{repo["days"]}}</td>
{%- endfor -%} {# repo #}
          </tr>
{%- endif %} {# packages defined #}
{%- endfor %} {# component #}
        </table>
{%- endfor -%} {# distribution #}

      <h2>Templates</h2>
        <table><tr><th>Template name</th><th>Version</th><th>Status</th></tr>
{%- for template, template_status in status[release].get("template", {}).items() %}
          <tr>
            <td>{{template}}</td><td>{{template_status["tag"]}}</td>
{%- for repo in template_status["repo"] %}
            <td class="{{color(repo["name"], repo["min-age-days"])}}">{{repo["name"]}}</td>
            <td class="{{color(repo["days"], repo["min-age-days"])}}">{{repo["days"]}}</td>
{%- endfor -%}
          </tr>
{%- endfor -%} {# template #}
        </table>

{%- endfor -%} {# release #}
  </body>
</html>
"""


def color(input_string, min_age_days=5):
    if input_string == "no version tag":
        tag = "no-version"
    elif isinstance(input_string, int):
        if int(input_string) > min_age_days:
            tag = "days-stable"
        else:
            tag = "days-testing"
    elif input_string == "built, not released":
        tag = "built-unreleased"
    elif input_string == "not released":
        tag = "unreleased"
    elif input_string == "":
        tag = "no-version"
    elif input_string == "":
        tag = "no-version"
    elif input_string in (
        "current",
        "current-testing",
        "security-testing",
        "unstable",
        "templates-itl",
        "templates-community",
        "templates-itl-testing",
        "templates-community-testing",
    ):
        tag = input_string
    else:
        raise ValueError(f"Unknown input string: {input_string}")
    return tag


def main():
    parser = ArgumentParser()
    parser.add_argument("--output-dir")
    input_parser = parser.add_mutually_exclusive_group()
    input_parser.add_argument("--input-dir")
    input_parser.add_argument("--use-existing-status-yaml-file")

    args = parser.parse_args()

    if not args.input_dir and not args.use_existing_status_yaml_file:
        raise ValueError(
            "Please provide either input directory or status.yml file."
        )

    if args.input_dir:
        input_dir = Path(args.input_dir).expanduser().resolve()
    else:
        input_dir = None

    if args.use_existing_status_yaml_file:
        status = yaml.safe_load(
            Path(args.use_existing_status_yaml_file)
            .expanduser()
            .resolve()
            .read_text()
        )
    else:
        status = {}

    if not args.output_dir:
        raise ValueError("Please provide output directory.")

    output_dir = Path(args.output_dir).expanduser().resolve()

    if input_dir:
        # component
        release_component_files = {}  # type: ignore
        for f in input_dir.glob("builder-*-status-component.yml"):
            release = re.match(
                r".*builder-(r[0-9.]*)-.*-status-component.yml", str(f)
            )
            if not release:
                continue
            release_component_files.setdefault(release.group(1), [])
            release_component_files[release.group(1)].append(f)

        # template
        release_template_files = {}  # type: ignore
        for f in input_dir.glob("builder-*-status-template.yml"):
            release = re.match(
                r".*builder-(r[0-9.]*)-.*-status-template.yml", str(f)
            )
            if not release:
                continue
            release_template_files.setdefault(release.group(1), [])
            release_template_files[release.group(1)].append(f)

        for release in list(release_component_files.keys()) + list(
            release_template_files.keys()
        ):
            status.setdefault(release, {"component": {}, "template": {}})

        for release in release_component_files:
            for f in release_component_files[release]:
                content = yaml.safe_load(f.read_text())
                for component in content:
                    for distribution in content[component]:
                        status[release]["component"].setdefault(
                            distribution, {}
                        )
                        status[release]["component"][distribution][
                            component
                        ] = content[component][distribution]

        for release in release_template_files:
            for f in release_template_files[release]:
                content = yaml.safe_load(f.read_text())
                for template in content:
                    status[release]["template"][template] = content[template]

        # Global minimum age days for stable.

        with open(output_dir / "statusv2.yml", "w") as fd:
            fd.write(yaml.dump(status))

    # render HTML
    template = Template(HTML_TEMPLATE)
    template.globals.update(color=color)
    html = template.render(status=status)

    with open(output_dir / "statusv2.html", "w") as fd:
        fd.write(html)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
