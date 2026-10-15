# compass-captain-hello

The example captain: one command, one API route, a tab, and a config schema.
It is the template for your own (`docs/writing_a_captain.md`).

Install it into a running Compass by adding it to `.env` and restarting:

```
COMPASS_EXTENSIONS=compass-captain-hello @ git+https://github.com/beamreach-ai/compass.git@v0.3.0#subdirectory=examples/captain-hello
```

It shows up under Settings → Captains, on by default, with a Hello tab drawn
by the generic captain page.
