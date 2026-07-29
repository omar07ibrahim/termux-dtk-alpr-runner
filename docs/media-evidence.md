# Verified media boundary

The media evidence lane is deliberately separate from DTK recognition. Its job
is to exercise a repository-generated synthetic video through a real FFmpeg
process, consume fixed-size RGB frames through the production process
supervisor, and publish only bounded receipts and visuals derived from those
frames.

It does not load a DTK library, use a camera, recognize a plate, or measure
accuracy, throughput, or latency.

## Pinned evidence runtime

The reproducible capture profile is Linux x86_64 with CPython 3.12.3 and the
`imageio-ffmpeg` 0.6.0 manylinux wheel in
[`requirements-media-evidence.lock`](../requirements-media-evidence.lock).
The wheel is accepted only at its PyPI SHA-256:

```text
c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282
```

That wheel contains `ffmpeg-linux-x86_64-v7.0.2`. The capture verifier also
checks the extracted executable before use:

```text
version: 7.0.2-static
bytes: 79826272
sha256: e7e7fb30477f717e6f55f9180a70386c62677ef8a4d4d1a5d948f4098aa3eb99
```

The wheel and executable are third-party tools, not Omar-authored artifacts.
The Python wrapper is distributed under BSD-2-Clause; the bundled FFmpeg build
reports GPLv3 configuration. Neither binary is committed or redistributed by
this repository. See the
[`imageio-ffmpeg` project](https://github.com/imageio/imageio-ffmpeg), its
[PyPI release](https://pypi.org/project/imageio-ffmpeg/0.6.0/), and
[FFmpeg licensing guidance](https://github.com/FFmpeg/FFmpeg/blob/master/LICENSE.md).

The lock intentionally supports only Linux x86_64 evidence capture. It is not
the Android/Termux installation contract and is never used to install or
activate the proprietary SDK.

## Local installation

Install into an ignored repository-local directory:

```bash
python3.12 -m pip install \
  --disable-pip-version-check \
  --require-hashes \
  --only-binary=:all: \
  --target .t/media-evidence-runtime \
  -r requirements-media-evidence.lock
```

No global AWS package, Docker configuration, Termux package, DTK binary,
license material, camera credential, or user media is changed by this step.

Run the public two-pass probe:

```bash
python3.12 -S tools/probe_media.py --json
```

The command renders the canonical Y4M source twice in memory, materializes one
copy in each of two separate `0700` workspaces under the dedicated
`.t/media-probe-workspaces/` root, invokes the production
`FfmpegFrameSource` supervisor twice, and requires byte-identical RGB frames
and lifecycle receipts. It does not change permissions on an existing `.t/`
directory. Each private Y4M file is mode `0600` and is removed before the
command returns.

The executable bytes are copied from the component-pinned, hash-checked runtime
file into a write-sealed Linux `memfd`; FFmpeg is executed through that exact
descriptor. Each rendered Y4M payload is independently copied into another
write-sealed descriptor and inherited as an already-open `pipe:N` input.
Consequently, pathname replacement between verification and use cannot change
the executable or input consumed by the two probe runs.

`InheritedFdArgument` treats those source descriptors as borrowed until lazy
start. It records file identity, status flags, and current offset without
opening a duplicate, so an abandoned, never-started supervisor owns no extra
FD. Under the guarded start path it first revalidates every borrowed signature,
reserves distinct private descriptor slots, atomically duplicates each source
into its already-owned slot, and closes the parent copies after process
adoption. The caller must keep each source FD open with unchanged flags and
offset until start; a missing FD or different signature fails before spawn.

Portable `fstat`/`fcntl`/`lseek` observations do not prove that two descriptors
are the identical open-file-description: a same-file reopen with exactly the
same observable signature cannot be distinguished. That case is outside the
generic borrowed-FD contract. The probe keeps its private write-sealed memfds
open, unmodified, and at the validated offset until guarded start; exact Y4M
and decoded-RGB digests remain the final content checks.

Main-thread `SIGINT` and `SIGTERM` request bounded supervisor cancellation.
The probe restores the caller's handlers, reaps the FFmpeg process group, and
removes both workspaces before returning a safe interruption error. `SIGKILL`,
`os._exit`, interpreter crashes, and kernel failure remain outside this
graceful-cleanup contract.

The closed FFmpeg profile disables host CPU dispatch with `-cpuflags 0`,
limits CPU and filter threads, requests bitexact processing, maps only the
single video stream, disables audio/subtitle/data streams, and emits `rgb24`
rawvideo. The public receipt replaces the executable and input paths with
`PINNED-FFMPEG-7.0.2` and `PRIVATE-SYNTHETIC-Y4M`.

The current exact identities are:

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| Canonical recipe | 1,288 | `ab8d7a7518d3d952d725ee96f0de9b6ecfdef7102f4b78ddcb125cab062466f6` |
| Rendered Y4M | 414,869 | `7614365d1342f1786ab82bb0d3fe07f1d5215cb6159bba40674234346ca7cfeb` |
| 18 decoded RGB24 frames | 829,440 | `51ccea55540f6c8e8e67ecd8ea11ba5a8f16f75f2b8cff438146090b171cc21a` |

The clean supervisor receipt reports 18 delivered frames, exit code `0`, zero
stderr bytes, no termination signal, and both process reaping and process-group
closure. It contains no PID, local path, hostname, timestamp, environment
value, or captured stderr text.

## Evidence and non-claims

The generated source video contains only deterministic geometric shapes. Its
closed, numeric-only recipe is
[`examples/synthetic-media-v1.json`](../examples/synthetic-media-v1.json);
`alpr_runner.synthetic_media` validates it and renders the exact YUV4MPEG2
bytes using only the Python standard library. The media probe establishes:

- the exact synthetic source bytes and recipe;
- the exact FFmpeg executable and sanitized command profile;
- complete fixed-size RGB frame delivery;
- per-frame byte identities and ordering;
- bounded buffering, stderr, timeouts, and process cleanup; and
- a receipt-bound decoded-frame source for the planned lossless visuals.

It cannot establish:

- DTK execution or recognition quality;
- camera, RTSP, Android, ARM64, or hardware compatibility;
- production FPS, latency, memory, or power consumption;
- a license grant for the repository-owned source; or
- authenticity of any external scene.

The repository still has no top-level source license. Until Omar chooses one,
the code and generated media remain all-rights-reserved despite being
reproducible and free of third-party or personal input data.
