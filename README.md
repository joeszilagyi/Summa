# Summa

## What This Is

Have you ever wondered if you could read everything ever written about something?

Have you ever read more than one book, article, newspaper story, television segment, or film about a topic and wanted to keep going? You tell Indexer what you want to learn about, and it runs in the background using your own tools, like ChatGPT or Claude, as research helpers. Say your topic is trout fly fishing in Montana. Indexer first finds and records the most well-known people, places, books, terms, sources, and loose threads around that topic, including things you may already recognize. Then each time it runs, it uses what it found before to look a little farther out, adding smaller details and new leads as it goes. Over time, you end up with a map of what can be found about that subject and where each trail seems to lead.

Broad-topic examples like `trout fly fishing in Montana` currently use the
checked-in `general.v1` domain pack. The current pack catalog, statuses, and
runtime notes live in [docs/project/DOMAIN_PACKS.md](docs/project/DOMAIN_PACKS.md).

Install the app, tell it your topic, choose whether to run it by hand or automatically, and it will handle the rest. As it searches, it collects information in databases on your own computer. You can browse those databases as local websites, without publishing your research online.

Think of it like building a shareable map of everything on your topic, scattered across old reports, databases, websites, books, television, films, newspapers, PDFs, notes, and half-forgotten references. It keeps track of the names, places, records, sources, and loose threads so you can see what exists, what has support behind it, and what still needs checking.

## Technical Overview

Local-first subject indexing and bibliography workspace.

This repository's public core is a topic-neutral indexing and bibliography system. It uses LLMs only for candidate enumeration, multilingual query planning, and source-lead discovery. LLM outputs are not sources, citations, reviewed facts, or publishable prose. Every usable source must be manually opened and read by a human before it can become an accepted work, evidence record, or public-safe export artifact.

Indexer is not a raw-payload archive. Raw PDFs, HTML pages, screenshots, WARC files, page images, audio/video captures, OCR text, and full extracted text are transient processing inputs by default. The durable object is the canonical source/work database record plus variant identity, controlled subjects, first-class authority records, capture, and extraction metadata.

The goal of the project is: where should I look next?
