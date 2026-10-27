"""Render Ollama units and nftables for native CI validation."""
import argparse
from pathlib import Path
from jinja2 import Environment, StrictUndefined
import yaml

parser = argparse.ArgumentParser()
parser.add_argument("output", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
role = root / "ops/ollama/roles/ollama"
values = yaml.safe_load((role / "defaults/main.yml").read_text())
values.update(yaml.safe_load((root / "ops/ollama/inventory/production.yml").read_text())["all"]["children"]["ollama_hosts"]["hosts"]["arsene"])
values.update(ollama_service_user={"stdout": "daemon"}, ollama_binary="/usr/bin/true")
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
env = Environment(undefined=StrictUndefined)
for source, dest in [("shared.service.j2", "ollama.service"),
                     ("ollama-access.nft.j2", "ollama-access.nft")]:
    (output / dest).write_text(env.from_string((role / "templates" / source).read_text()).render(values))
# Native unit verification checks ordering dependencies as well as directives.
(output / "ollama-access.service").write_text((role / "files/ollama-access.service").read_text())
print(f"Rendered Ollama configuration to {output}")
