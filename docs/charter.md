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
- FFmpeg raw-RGB frame delivery over a pipe.
- A multi-camera worker model with per-camera recognition engines.
- Cross-camera plate aggregation and software zoom targeting.
- Loopback-only status dashboard and JSON/image runtime outputs.
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

## Evidence policy

Screenshots and performance charts must come from an actual recorded run and
must state its hardware, software, media, and proprietary-SDK boundaries.
Hand-authored diagrams may explain architecture, but they must be labeled as
architecture rather than runtime proof. Real plate numbers, private camera
addresses, license material, and host-specific paths must never be committed.
