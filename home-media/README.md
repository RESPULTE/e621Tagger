# Home Media tagging worker

This subproject runs Hydra tagging on the local GPU. It reads media from an explicitly
allowed home-server path over SFTP and updates Stash through an SSH-forwarded API
connection. Original media stays on the server.

## Setup

Run `scripts/bootstrap-worker.ps1` from this directory to create the Python environment,
install the CUDA and Hydra dependencies, and verify the model in the parent project's
`models/` directory. Copy `config.example.toml` to `config.toml`, then set the Stash API
key, SSH connection, and allowed media roots for your server.

Use `scripts/run-worker.ps1` to start or resume processing. The connection-check script
only checks for queued requests; it requires an approval prompt before starting GPU work.
The scripts in `scripts/` manage local worker setup and control. `plugin/` contains the
Stash-side queue extension.

The local `config.toml`, `data/`, virtual environment, and credentials are ignored by Git.
