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
- Runtime directory leaves are non-symlink directories tightened to mode
  `0700`; JSON replacement rejects symlink and special-file destinations and
  writes mode `0600` files atomically.
- Runtime images are encoded into bounded memory buffers and then published
  through descriptor-relative atomic replacement at mode `0600`; symlink and
  special-file destinations fail closed. Generated HTML uses the same private
  atomic file boundary.
- Status records use source-kind descriptors instead of camera URLs or host
  paths, and the still-image runner no longer prints the SDK system ID.
- FFmpeg-video input rejects non-byte or wrong-sized RGB24 frames before any
  native call. Native buffers have no implicit trailing byte and stay paired
  with their immutable payload by callback timestamp, never by a global
  "latest frame" fallback.
- The production `ffmpeg_video` raw-RGB path uses the bounded supervisor
  directly. Frame geometry and timeout/stderr limits are validated before
  runtime-directory or native-library side effects; startup and inter-frame
  progress have separate deadlines; stderr bytes are capped and never copied
  into status, receipts, or public exceptions.
- In that raw-RGB path, FFmpeg runs in a dedicated process group with stdin
  disabled. Shutdown must confirm producer reaping and group quiescence before
  DTK destruction, and DTK destruction must succeed before frame leases are
  released. Body and cleanup failures are preserved together rather than
  replacing one another.
- The main-thread raw-RGB runner converts `SIGINT` and `SIGTERM` into a
  reentrant, lock-free stop flag. The supervisor observes that flag on bounded
  polling intervals, performs the same source-to-DTK-to-lease teardown, and
  restores the caller's previous signal handlers. Repeated signals do not call
  `threading.Event.set()` from the Python signal handler.
- Video-frame leases are bounded by both count and retained bytes. The default
  is 16 leases and a 96 MiB ceiling; payloads borrowed by overlapping plate
  callbacks remain charged after completion until the final exact borrow is
  released.
- Exceptions at the `ctypes` callback boundary are contained as stable,
  source-free failure codes, request an ingestion stop, and cannot bypass the
  exact-once native plate-destruction path.
- Expected SDK initialization failures, including the default license check,
  destroy any allocated engine and parameter handles. A failure while
  registering the completed-frame callback rolls back both the inert FFmpeg
  source and the native owner before constructor failure is published.
- Runtime directories and vendor directories have dedicated ignore rules.
- The runner stops when the SDK reports an unlicensed state unless the operator
  explicitly enables the development-only override.
- The public evidence path accepts only bounded, strictly shaped `SYNTH-*`
  events, uses no image or recognition backend, and writes its runtime artifact
  inside a mode-`0700` directory as a mode-`0600` file.
- The Linux evidence renderer runs each fixed Python 3.12 command twice with a
  minimal environment, bounded stdout/stderr, and a timeout. Before spawn it
  requires one main kernel thread, the default `SIGCHLD` disposition, no
  pre-existing children, and a successful wait-status canary; a temporary
  child subreaper contains both declared no-detach commands and the
  signal-aware media wrapper. Numeric process-group signals are permitted only
  while `waitid(..., WNOWAIT)` still anchors the leader, and adopted descendants
  are killed and reaped before the subreaper state is restored. Parent
  `SIGHUP`, `SIGINT`, `SIGQUIT`, and `SIGTERM` become bounded cleanup requests.
  Those signals must be unblocked before entry so children cannot inherit a
  mask that defeats the cleanup contract. The renderer requires byte-identical
  results and exact canonical-summary stdout before deriving any visual.
- The synthetic-media probe accepts only the exact numeric recipe and a
  size/hash-pinned Linux x86_64 FFmpeg executable. It copies the verified
  executable into a write-sealed `memfd`, passes each independently rendered
  Y4M source through a separate write-sealed inherited descriptor, and uses
  the production frame supervisor for exact RGB24 delivery. Pathname swaps
  cannot change the bytes executed or consumed after verification.
- Descriptor-bound command arguments snapshot borrowed file identity, status
  flags, and current offset without creating constructor-lifetime duplicates.
  Lazy start revalidates those observable signatures, reserves distinct private
  FD slots, and atomically replaces each reservation with its inherited copy;
  missing or observably changed descriptors therefore fail before spawn. This
  does not portably prove an exact open-file-description, so callers must retain
  the original FD unchanged until start.
- Media-probe workspaces live only below a dedicated mode-`0700` directory;
  their Y4M materializations are mode `0600`. Main-thread `SIGINT`/`SIGTERM`
  use a reentrant flag, bounded process-group cleanup, handler restoration,
  and workspace removal.
- Generated evidence has an exact allowlist. Symlinks, special files, and
  unexpected entries are rejected; inputs are hashed before and after the
  runs; the private staging root stays pinned by file descriptor; published
  bytes receive privacy and host-identity scans; accessible inert SVG structure
  is validated; and the evidence manifest is replaced last.

## Known gaps

- Camera URLs are command-line arguments and remain visible in the runner and
  FFmpeg OS process arguments. The supervisor prevents those arguments and
  FFmpeg diagnostic contents from entering its receipts or exceptions, but it
  is not a credential provider or process-list privacy boundary.
- Runtime status records and images still contain recognition data.
- The dashboard has no authentication, authorization, TLS, CSRF defense,
  retention control, or response-security headers.
- Other FFmpeg launch paths do not yet share the raw-RGB supervisor contract.
  In particular, the dashboard snapshot path still captures FFmpeg stderr
  without the same explicit timeout and byte cap.
- Diagnostics written directly by the proprietary in-process SDK also remain
  outside the FFmpeg supervisor's bounded stderr contract.
- The graceful-interruption contract covers ordinary synchronous exceptions
  and real process `SIGINT`/`SIGTERM` after the main-thread runner installs its
  handlers. It cannot make instruction-level atomicity guarantees against
  `SIGKILL`, `os._exit`, interpreter/native crashes, or artificial
  `PyThreadState`/trace-hook exception injection between arbitrary CPython
  bytecodes. A monkeypatched spawn wrapper that creates a child and then raises
  before returning its owner is likewise outside the contract; guessing a PID
  from a concurrent process table could terminate an unrelated child. Real
  Python-handled signals are masked across descriptor binding, spawn, and
  ownership publication. Standalone supervisor callers remain responsible for
  calling `close()` if their own control flow abandons a live source.
- Plate aggregation keeps plaintext plate keys in memory and on disk.
- The SDK system identifier may be printed by existing launch paths.
- The proprietary SDK's callback ABI, timestamp propagation, frame-ownership
  transfer, terminal-completion boundary, and Destroy quiescence cannot be
  verified from this repository. The wrapper conservatively retains memory
  through callback overlap, but licensed-device conformance remains required.
- There is no complete production/Termux dependency lock, SBOM, signed
  release, or vulnerability policy. Only the Linux x86_64 evidence runtime is
  hash-locked by `requirements-media-evidence.lock`.

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

The committed synthetic-event bundle satisfies these publication rules for its
event-level boundary. It contains no media and therefore offers no evidence
about recognition output, camera behavior, SDK performance, or pixel handling.
The explanatory architecture and setup diagrams are labeled separately from
runtime-derived event-flow and geometry artifacts. Terminal evidence is marked
as a path-normalized rendering of exact stdout rather than a literal screen
capture.

The separate synthetic-media probe now establishes exact real FFmpeg RGB24
delivery and clean supervisor lifecycle behavior without a camera or SDK. It
does not establish recognition, detection, accuracy, performance, or hardware
compatibility.

## Planned mitigations

- Redacted source descriptors at every log and status boundary.
- Private-mode, atomic runtime storage with an explicit retention command.
- Opt-in exposure of full plate text, disabled for portfolio evidence.
- A camera credential provider or descriptor-based FFmpeg input boundary that
  does not place authenticated RTSP URLs in OS process arguments.
- Migrate the dashboard snapshot and any remaining FFmpeg launch paths to the
  same bounded supervision and source-safe diagnostic contract.
- Strict request routing and security headers for the dashboard.
- Licensed-device conformance tests for the documented DTK ownership and
  callback-order assumptions; offline tests use opaque fake handles and prove
  only the repository-owned wrapper model.
