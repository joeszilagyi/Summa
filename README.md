# Summa

## What This Is

Have you ever wondered if you could read everything ever written about something?

Have you ever read more than one book, article, newspaper story, television segment, or film about a topic and wanted to keep going? You tell Indexer what you want to learn about, and it runs in the background using your own tools, like ChatGPT or Claude, as research helpers. Say your topic is trout fly fishing in Montana. Indexer first finds and records the most well-known people, places, books, terms, sources, and loose threads around that topic, including things you may already recognize. Then each time it runs, it can use bounded prior canonical state from earlier cycles to look a little farther out, adding smaller details and new leads as it goes. Over time, you end up with a map of what can be found about that subject and where each trail seems to lead.

Broad-topic examples like `trout fly fishing in Montana` currently route to the
checked-in `general.v1` domain pack. That routing is not yet fixture proof that
`general.v1` safely covers every place-dominant recreation shape end to end;
the current tested-coverage note lives in
[docs/project/DOMAIN_PACKS.md](docs/project/DOMAIN_PACKS.md).

The current gather driver keeps one-shot mode as the default. Prior canonical
state is injected only when `tools/scripts/run_topic_gather.py` is invoked with
its explicit prior-state flags, and proposed or needs-review rows remain leads,
not accepted facts.

When the canonical store already has subject history, the feedback planner can
now score productive facets and open leads, emit a concrete next-action plan,
and feed that plan back into `run_topic_gather.py`. That selection still ranks
where to look next rather than deciding what is true: proposed and
needs-review rows remain leads, not accepted facts.

Install the app, tell it your topic, choose whether to run it by hand or automatically, and it will handle the rest. As it searches, it collects information in databases on your own computer. You can browse those databases as local websites, without publishing your research online.

Think of it like building a shareable map of everything on your topic, scattered across old reports, databases, websites, books, television, films, newspapers, PDFs, notes, and half-forgotten references. It keeps track of the names, places, records, sources, and loose threads so you can see what exists, what has support behind it, and what still needs checking.

## Technical Overview

Local-first subject indexing and bibliography workspace.

This repository's public core is a topic-neutral indexing and bibliography system. It uses LLMs only for candidate enumeration, multilingual query planning, and source-lead discovery. LLM outputs are not sources, citations, reviewed facts, or publishable prose. Every usable source must be manually opened and read by a human before it can become an accepted work, evidence record, or public-safe export artifact.

Indexer is not a raw-payload archive. Raw PDFs, HTML pages, screenshots, WARC files, page images, audio/video captures, OCR text, and full extracted text are transient processing inputs by default. The durable object is the canonical source/work database record plus variant identity, controlled subjects, first-class authority records, capture, and extraction metadata.

The goal of the project is: where should I look next?
