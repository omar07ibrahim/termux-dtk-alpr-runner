# Threat model and data boundary

## Protected data

Depending on the deployment, runtime data may include:

- camera URLs and embedded credentials;
- license plates and vehicle attributes;
- images, crops, and timestamps;
- device or SDK system identifiers;
- camera names, filesystem paths, and network locations;
- proprietary SDK files, license keys, and activation material.

Treat all of these as sensitive unless a synthetic fixture proves otherwise.

## Trust boundaries

```text
camera or media
      |
      v
FFmpeg / DTK capture ----> proprietary native SDK
      |                           |
      +-------- raw frames -------+
                                  |
                                  v
                      Python callbacks and aggregation
                                  |
                     +------------+------------+
                     |                         |
                     v                         v
               runtime files          loopback dashboard
```

The native SDK, FFmpeg input parser, camera endpoint, and supplied media are
outside the repository's trust boundary. Native calls execute in-process, so a
memory-safety defect in the SDK can compromise the runner.

## Current controls

- Proprietary SDK and activation files are ignored rather than committed.
- The dashboard binds to numeric loopback by default.
- Subprocesses use argument arrays instead of shell interpolation.
- Runtime directories and vendor directories have dedicated ignore rules.
- The runner stops when the SDK reports an unlicensed state unless the operator
  explicitly enables the development-only override.

## Known gaps

- Camera URLs are command-line arguments and may appear in process listings.
- Some runtime status records include full source strings and recognition data.
- Runtime files do not yet enforce private directory and file modes.
- The dashboard has no authentication, authorization, TLS, CSRF defense,
  retention control, or response-security headers.
- FFmpeg and native SDK stderr are not consistently bounded or sanitized.
- Plate aggregation keeps plaintext plate keys in memory and on disk.
- The SDK system identifier may be printed by existing launch paths.
- There is no dependency lock, SBOM, signed release, or vulnerability policy.

Until these gaps are closed, use only a single-user, isolated, local test
environment with synthetic or explicitly authorized media.

## Publication rules

Committed evidence must:

1. use synthetic plate strings and redistributable media;
2. remove camera authority, user information, query strings, and filesystem
   roots;
3. exclude SDK system IDs, license state, activation output, and secrets;
4. record whether values came from a real run, a deterministic fixture, or a
   hand-authored architecture description; and
5. pass an automated scan for host paths, credential forms, and unexpected
   generated files.

## Planned mitigations

- Redacted source descriptors at every log and status boundary.
- Private-mode, atomic runtime storage with an explicit retention command.
- Opt-in exposure of full plate text, disabled for portfolio evidence.
- Bounded, timeout-aware FFmpeg supervision with process-group cleanup.
- Strict request routing and security headers for the dashboard.
- Pure-Python contract tests with a fake native adapter.
- A synthetic fixture/evidence pipeline that never requires proprietary data.
