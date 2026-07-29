# Project charter

## Purpose

Termux DTK ALPR Runner explores a narrow systems problem: feeding local camera
frames from Android/Termux into a proprietary Linux ARM64 recognition engine,
then coordinating multiple streams and deriving a software crop command from
the returned plate region.

The portfolio value is the integration boundary—not a claim that this
repository owns, redistributes, or independently validates the recognition
model.

## Implemented

- Python `ctypes` bindings for the DTK image and video interfaces used here.
- Production FFmpeg raw-RGB delivery through the bounded supervisor: strict
  frame geometry, startup and inter-frame deadlines, a private stderr cap,
  stable source-safe receipts, reentrant `SIGINT`/`SIGTERM` stop handling, and
  confirmed process-group cleanup before native teardown.
- A multi-camera worker model with per-camera recognition engines.
- Cross-camera plate aggregation and software zoom targeting.
- A strict, deterministic fake-event CLI that reuses the production
  aggregation and zoom-geometry modules without loading images or an SDK.
- Loopback-only status dashboard and JSON/image runtime outputs.
- Private-mode runtime directories and files with redacted source descriptors.
- Exact-ID RGB frame leases with count and byte backpressure, opaque-handle
  callback tests, sanitized callback failures, and no latest-frame preview
  fallback. Expected license and callback-registration failures roll back
  partially constructed native owners.
- A reproducible evidence renderer with two byte-identical CLI runs, bounded
  subprocess streams, Linux subreaper containment, graceful parent-signal
  cleanup, PID/PGID ownership checks, exact generated-tree checks, source
  hashes, privacy scans, accessible SVG validation, and manifest-last
  publication.
- A Linux x86_64 synthetic-media probe that binds a hash-pinned FFmpeg
  executable and both Y4M renders from the numeric-only recipe to write-sealed
  descriptors, delivers 18 exact RGB24 frames twice through the production
  supervisor, and emits a path-free lifecycle receipt.
- Termux/proot setup and launch scripts.

## Required external components

- A legitimately obtained DTK Linux ARM64 SDK and license.
- A compatible AArch64 Linux userspace.
- FFmpeg and the Python packages imported by the selected runner.
- An authorized file, device, or RTSP camera source.

These components are not vendored or fetched automatically by the repository.

## Current non-claims

- No recognition-accuracy, false-positive, or false-negative benchmark.
- No reproducible throughput or latency benchmark.
- No Android Camera2 hardware zoom or PTZ control.
- No secure credential provider for authenticated camera streams.
- No authentication, authorization, TLS, or multi-user dashboard boundary.
- No retention, encryption, redaction, or deletion policy for runtime data.
- No claim that a proprietary SDK result is independently reproducible.
- No claim that offline fakes prove the licensed SDK's callback ABI, timestamp
  propagation, terminal completion, ownership transfer, or Destroy quiescence.
- The committed synthetic evidence executes one canonical validated event
  trace through orchestration, aggregation, and normalized zoom geometry. It
  does not prove exhaustive validation, recognition, media decoding, camera
  integration, accuracy, latency, or throughput.
- The synthetic-media probe proves only pinned FFmpeg frame delivery and
  lifecycle cleanup for one 160×96 geometric source. It does not execute DTK,
  recognize or detect anything, use a camera, or measure performance.

## Portfolio-grade release criteria

Before a release is described as reproducible, the repository must have:

1. a dependency and supported-runtime contract;
2. automated tests that do not require proprietary binaries;
3. bounded subprocess and frame-buffer behavior with explicit failure states;
4. default-safe handling of camera URLs, device identifiers, and plate text;
5. synthetic, redistributable fixtures with exact provenance;
6. real CLI/dashboard captures derived from those fixtures;
7. architecture, data-lifecycle, and failure-path diagrams;
8. a machine-readable evidence manifest and byte-current visual checks; and
9. a declared license for the repository-owned source.

The event-level evidence bundle now covers criterion 2, part of 6, part of 7,
and criterion 8 for its vendor-independent boundary. Repository-owned FFmpeg
supervision and frame-buffer ownership now cover the raw-RGB runner's portion
of criterion 3 with bounded, fail-closed contracts and focused offline tests;
other FFmpeg launch paths and licensed-device ownership/callback-quiescence
assumptions still require hardening or conformance testing. Criterion 5 is not
complete because the repository-owned fixture has no granted redistribution
license yet. The bundle does not make the broader camera/SDK project
reproducible: the dependency contract, dashboard evidence, remaining
subprocess and licensed-device failure paths, and repository license remain
release work.

## Evidence policy

Screenshots and performance charts must come from an actual recorded run and
must state its hardware, software, media, and proprietary-SDK boundaries.
Hand-authored diagrams may explain architecture, but they must be labeled as
architecture rather than runtime proof. Real plate numbers, private camera
addresses, license material, and host-specific paths must never be committed.

The current bundle uses only `SYNTH-01`, `SYNTH-02`, and
`SYNTH-CAM-01`/`SYNTH-CAM-02`. Its terminal evidence is rendered from exact
canonical-summary stdout with a path-normalized command and exit marker added
by the renderer; its event-flow and zoom figures are derived from the actual
JSON artifact; its architecture and setup figures are explicitly explanatory.
The event lane has no GIF or video because its fixture has no pixels and
animation would not establish an additional verified property. The separate
exact RGB media probe publishes a lossless contact sheet and all-frame GIF from
real decoded pixels; both are decoded and checked byte-for-byte against the
receipt while preserving the same non-recognition boundary.
