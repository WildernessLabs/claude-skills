# claude-skills

Claude Code skills from Wilderness Labs, distributed as a plugin marketplace.

## Plugins

| Plugin | Description |
| --- | --- |
| `embedded-debugger` | Drive GDB against a physical MCU over JTAG/SWD (OpenOCD, probe-rs, pyOCD) and capture its serial console directly to diagnose hangs, crashes, privilege and interrupt faults. Includes notes for Meadow, ESP32 and STM32 targets. |

## Installing

In Claude Code, add the marketplace and install the plugin:

```
/plugin marketplace add WildernessLabs/claude-skills
/plugin install embedded-debugger@wildernesslabs-skills
```

Restart Claude Code (or run `/reload-plugins`) and the skill is available. Claude uses it automatically when you ask it to debug firmware on real hardware, or you can invoke it by name.

### Updating

```
/plugin marketplace update wildernesslabs-skills
```

Then update the plugin from the `/plugin` menu. You can also enable auto-update for the marketplace there.

### Requirements

The skill drives tools you must already have installed: a GDB for your target (e.g. `arm-none-eabi-gdb`, `xtensa-esp32-elf-gdb`), a debug server such as OpenOCD, probe-rs or pyOCD, and Python 3 for the serial capture helpers in `tools/`.

## Developing

Edit the skill in `plugins/embedded-debugger/skills/embedded-debugger/`.

### Working on the skill while using it

To use the skill day to day and edit it at the same time, link your personal skills directory to this clone instead of installing the plugin. Claude Code loads skills from `~/.claude/skills/`, so a symlink there means every edit in the repository is live in the next session, with no reinstall or publish step:

```
git clone https://github.com/WildernessLabs/claude-skills.git
cd claude-skills
ln -s "$PWD/plugins/embedded-debugger/skills/embedded-debugger" ~/.claude/skills/embedded-debugger
```

Check the link with `ls -l ~/.claude/skills/embedded-debugger`. It should point at the `skills/embedded-debugger` folder in your clone. Changes you make there, including through Claude in another session, land in the repository, ready to review and commit.

Use either the symlink or the marketplace plugin, not both. With both installed, Claude sees two copies of the skill and may load the published one rather than your edited one. If you installed the plugin first, remove it with `/plugin uninstall embedded-debugger@wildernesslabs-skills` before creating the link. To go back to the published version, delete the link (`rm ~/.claude/skills/embedded-debugger`, which removes only the link, not your clone) and install the plugin as above.

### Testing as a plugin

To check that the skill works when packaged as a plugin, for example before publishing a version, load the plugin directory for one session:

```
claude --plugin-dir ./plugins/embedded-debugger
```

Bump `version` in `plugins/embedded-debugger/.claude-plugin/plugin.json` when publishing so installed copies update.

## License

MIT - see [LICENSE](LICENSE).
