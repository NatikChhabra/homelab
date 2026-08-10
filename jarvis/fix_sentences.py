#!/usr/bin/env python3
"""Fix HA custom sentences to accept bare 'feeling hot' / 'feeling cold'."""
import yaml
import sys

PATH = "/config/custom_sentences/en/jarvis.yaml"

with open(PATH, "r") as f:
    content = f.read()

# Add bare "feeling hot" to JarvisFeelingHot
old_hot = '          - "(mujhe |)garmi lag rahi hai"'
new_hot = '''          - "(mujhe |)garmi lag rahi hai"
          - "feeling hot"
          - "feeling hot please"
          - "i feel hot"
          - "it is hot"'''
if "feeling hot" not in content:
    content = content.replace(old_hot, new_hot)
    print("Added bare 'feeling hot' to JarvisFeelingHot")
else:
    print("Already has 'feeling hot'")

# Add bare "feeling cold" to JarvisFeelingCold
old_cold = '          - "(mujhe |)thand lag rahi hai"'
new_cold = '''          - "(mujhe |)thand lag rahi hai"
          - "feeling cold"
          - "feeling cold please"
          - "i feel cold"
          - "it is cold"'''
if "feeling cold" not in content:
    content = content.replace(old_cold, new_cold)
    print("Added bare 'feeling cold' to JarvisFeelingCold")
else:
    print("Already has 'feeling cold'")

with open(PATH, "w") as f:
    f.write(content)

print("Done. Restart jarvis-hass to apply.")
