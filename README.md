# Nanome Deployment Scripts

View latest deployment instructions at [nanome.ai/deploy](https://nanome.ai/deploy)

## Infrastructure

- Nanome server (Ubuntu or Linux)

## Deployment

### Setup

Log in to the server via SSH and run the following commands
(step-by-step explanation in next section)

```sh
sudo apt-get update
sudo apt install -y git python3 python3-venv
git clone https://github.com/nanome-ai/nanome-deployer
cd nanome-deployer
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python install_prereqs.py
sudo reboot
```

After reboot
```sh
cd nanome-deployer
source venv/bin/activate
python setup.py
docker compose up -d --wait
```

The setup script will ask what you want to deploy:

1. **Everything** — workspaces + web (MARA)
2. **Web only** — MARA, tool server, and embeddings
3. **Workspaces only** — Workspace API, database, and auth proxy

Your choice is recorded in the root `.env` (via `COMPOSE_PROFILES`), so the
same `docker compose up -d --wait` command deploys the right services for
every mode — no extra flags needed. Re-run `python setup.py` at any time to
change modes or settings; if you remove a service group, follow the script's
prompt to run `docker compose down --remove-orphans` before starting again.

### Update / Redeploy

```sh
cd nanome-deployer
python aws_login.py
docker compose up -d --wait
```

### Setup Step-by-step

1) Install git and python
   ```sh
   sudo apt-get update
   sudo apt install -y git python3 python3-venv
   ```

2) Clone this repository
   ```sh
   git clone https://github.com/nanome-ai/nanome-deployer
   cd nanome-deployer
   ```

3) Set up Python and pip
   ```sh
   python3 -m venv venv
   source venv/bin/activate
   ```

4) Install script dependencies, Docker, and AWSCLI
   ```sh
   pip install -r requirements.txt
   python install_prereqs.py
   ```

5) Reboot the server
   ```sh
   sudo reboot
   ```

6) Reactivate virtualenv
   ```sh
   cd nanome-deployer
   source venv/bin/activate
   ```

7)  Run setup script and start services
    ```sh
    python setup.py
    docker compose up -d --wait
    ```

### Customization

- **Hostnames**: `setup.py` asks for the hostname of each service (default `nanome.<domain>`, `nanome-tools.<domain>`, ...). Later runs keep the values you set. For individual TLS certificates, name each file after the first label of its hostname (e.g. `mara.crt`/`mara.key` for `mara.example.com`).
- **Corporate CA**: if your network needs extra CA certificates (e.g. SSL inspection), give the PEM file when `setup.py` asks. It is copied to `certs/ca.pem` and added to `certs/bundle.pem`, which services use through `REQUESTS_CA_BUNDLE` and `SSL_CERT_FILE`.
- **Compose overrides**: put other changes in `docker-compose.override.yaml` (see `docker-compose.override.example.yaml`). `docker compose` loads it automatically, and git ignores it, so `git pull` keeps it.

### Troubleshooting

- **"An error occurred (InvalidClientTokenId) when calling the GetCallerIdentity operation" during setup**
  The AWS credentials were likely entered incorrectly. You'll have the opportunity to reenter them when you run `setup.py`. Alternatively, you can edit them directly by opening `~/.aws/credentials` in a text editor.

- **"Address already in use" error when starting services**
  Another program on the server is already using port 80. To find out what it is, run:
  ```sh
  sudo lsof -i :80
  ```
  If the program is nginx (a web server sometimes included in the base VM image), you can remove it and restart:
  ```sh
  sudo apt remove nginx nginx-common
  docker compose down
  docker compose up -d
  ```

- **MARA returns a "certificate verify failed" error when running some tools like Download from RCSB**
  Your network may be using SSL introspection, which interferes with outbound requests. Re-run `python setup.py` and give your corporate CA certificate when asked (see Customization). Alternatively, ask your IT team for an exemption, or contact Nanome support for a list of domains to allowlist (e.g. rcsb.org).

- **Web UI shows "Failed to fetch" or you cannot create a workspace**
  The Workspace API service may not be running. Check its logs for errors:
  ```sh
  docker compose logs workspace-api
  ```
  Also verify that the URL in your `.env.workspaces` file is correct.
