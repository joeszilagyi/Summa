# Public Safekeeping Manifest

`public-safekeeping-manifest.v1` records the exact files in a local public
sharing bundle that are suitable for Git, archive, or manual preservation.

It exists for operator handoff, not for automatic publishing. The manifest must
state `upload_attempted: false`, and the builder for this manifest must not
create remotes, push commits, upload archives, or mutate third-party accounts.

## Required contents

- hash and size for every preserved artifact
- explicit `rights_posture` per artifact
- preservation channels suitable for manual handoff
- copied `excluded_families` context from the public sharing bundle
- manual operator instructions for Git and archive preservation

## Manual operator path

1. Build and validate the public sharing bundle locally.
2. Build the safekeeping manifest from that bundle.
3. Review the manifest hashes and excluded families before any handoff.
4. If Git preservation is desired, commit the bundle directory manually in a
   chosen repository after policy review.
5. If archive preservation is desired, create an archive file manually after
   verifying the manifest hashes.
6. Handle any public upload as a separate, explicit operator decision outside
   this toolchain.
