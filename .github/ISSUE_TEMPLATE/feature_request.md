---
name: Feature request
about: A data source, a score change, or an MCP tool
title: ""
labels: enhancement
---

**Problem**

Who on the ground needs this, and what decision does it change?

**Proposal**

If this is a new feed, it should implement `fetch(bbox, time_range) -> SourceResult`. If an agent should call it, add a tool in `wildfirebot.tools` rather than only a CLI flag.

**Milestone**

Say whether this is milestone 2 (the PM2.5 model) or milestone 3 (the MCP server and map).
