import sys
import certifi
import getpass
import os
import random
import string
import urllib.request
import yaml

PLAYBOOKS_DIR = os.path.join(os.path.dirname(__file__), 'playbooks')


def masked_input(prompt: str) -> str:
    """Read a line from stdin, echoing ``*`` per character.

    Unlike ``getpass.getpass`` (silent), this lets the operator see the
    length of what they typed or pasted. Falls back to ``getpass.getpass``
    if stdin is not a tty (CI, piped input).
    """
    if not sys.stdin.isatty():
        return getpass.getpass(prompt)

    sys.stdout.write(prompt)
    sys.stdout.flush()

    if sys.platform == 'win32':
        import msvcrt
        buf = []
        while True:
            ch = msvcrt.getwch()
            if ch in ('\r', '\n'):
                sys.stdout.write('\n')
                sys.stdout.flush()
                return ''.join(buf)
            if ch == '\x03':  # Ctrl-C
                sys.stdout.write('\n')
                raise KeyboardInterrupt
            if ch in ('\b', '\x7f'):
                if buf:
                    buf.pop()
                    sys.stdout.write('\b \b')
                    sys.stdout.flush()
                continue
            if ch == '\x00' or ch == '\xe0':
                # function/arrow keys send a two-byte sequence; consume the
                # second byte and ignore — we don't support cursor movement.
                msvcrt.getwch()
                continue
            buf.append(ch)
            sys.stdout.write('*')
            sys.stdout.flush()

    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        buf = []
        while True:
            ch = sys.stdin.read(1)
            if ch in ('\r', '\n'):
                sys.stdout.write('\n')
                sys.stdout.flush()
                return ''.join(buf)
            if ch == '\x03':  # Ctrl-C
                sys.stdout.write('\n')
                raise KeyboardInterrupt
            if ch in ('\x7f', '\b'):
                if buf:
                    buf.pop()
                    sys.stdout.write('\b \b')
                    sys.stdout.flush()
                continue
            # Strip ASCII control chars (incl. start of escape sequences from
            # arrow keys or bracketed-paste markers) so they don't render as
            # asterisks. Real password chars are all printable.
            if ord(ch) < 32:
                continue
            buf.append(ch)
            sys.stdout.write('*')
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def ask_selection(question, options, default=None):
    """Prompt the user to select from numbered options.

    Args:
        question: The question text to display.
        options: List of option labels (displayed as 1., 2., etc.).
        default: 1-indexed default option number, or None.

    Returns:
        The selected option number as a string ('1', '2', etc.).
    """
    option_lines = '\n'.join(f'{i}. {opt}' for i, opt in enumerate(options, 1))
    default_hint = f' [{default}]' if default else ''

    valid = [str(i) for i in range(1, len(options) + 1)]
    if default:
        valid.append('')

    selection = None
    while selection not in valid:
        selection = input(
            f'\n\n{question}\n'
            f'{option_lines}\n\n'
            f'Make a selection{default_hint}: '
        )

    return selection if selection != '' else str(default)


def base_domain(hostname):
    """Strip the service label, e.g. 'nanome.company.com' -> 'company.com'."""
    return hostname.split('.', 1)[1] if '.' in hostname else None


def cert_name(hostname):
    """Individual cert files are named after the hostname's first label."""
    return hostname.split('.', 1)[0]


def gather_host_info(default=None):
    default_hint = f'[{default}]' if default else '(defaults to <IP>.nip.io)'
    host = input(f'\n\nWhat domain name will you be using to access? (e.g. example.com)\nEnter domain {default_hint}: ')
    if not host and default:
        host = default
    elif not host:
        host = get_public_ip() + '.nip.io'
    return host


def generate_random_password(length=16):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))


def check_certs(path, name, enforce=False):
    for ext in ['crt', 'key']:
        cert_path = os.path.join(path, f'{name}.{ext}')
        if not os.path.exists(cert_path):
            return False
        # check if PEM format
        with open(cert_path, 'r') as f:
            content = f.read()
            if ext == 'crt' and '-----BEGIN CERTIFICATE-----' not in content:
                if enforce:
                    print(f"Certificate {name}.{ext} must be in PEM format.")
                    sys.exit(1)
                return False
            if ext == 'key' and '-----BEGIN PRIVATE KEY-----' not in content:
                if enforce:
                    print(f"Private key {name}.{ext} must be in PEM format.")
                    sys.exit(1)
                return False
    return True


def has_default_certs(path, enforce=False):
    return check_certs(path, 'default', enforce=enforce)


def has_individual_certs(path, names, enforce=False):
    return all(check_certs(path, name, enforce=enforce) for name in names)


def _copy_file(src, dest):
    with open(src, 'rb') as src_file, open(dest, 'wb') as dest_file:
        dest_file.write(src_file.read())


def gather_https_info(host, hostnames):
    """Ask for TLS certs and copy them to ./certs.

    ``hostnames`` are the service hosts that need individual certs.
    Returns 'Default', 'Individual', or 'None'.
    """
    cert_names = [cert_name(h) for h in hostnames]
    use_existing = None
    has_default = has_default_certs('./certs')
    has_individual = has_individual_certs('./certs', cert_names)
    if has_default or has_individual:
        use_existing = ask_selection(
            "Found existing TLS certificates in ./certs\n"
            "Would you like to use these for HTTPS?",
            ['Yes', 'No'],
            default=1,
        )

    if use_existing != '1':
        https_input = ask_selection(
            "Do you have local TLS certificates you would like to use for HTTPS?",
            ['Yes', 'No'],
            default=2,
        )
        if https_input != '1':
            return 'None'

    while use_existing != '1':
        individual_lines = ''.join(
            f"  {cert_name(h)}.crt/{cert_name(h)}.key covering {h}\n" for h in hostnames
        )
        certs_path = input(
            "\n\nEnter path containing either:\n"
            f"  default.crt/default.key covering *.{host}\n"
            "or\n"
            + individual_lines
            + "\nEnter path: "
        )

        has_default = has_default_certs(certs_path)
        has_individual = has_individual_certs(certs_path, cert_names)
        if not (has_default or has_individual):
            print("Could not find valid certificates in the provided path. Please try again.")
            continue

        os.makedirs('./certs', exist_ok=True)
        names = ['default'] if has_default else cert_names
        for name in names:
            check_certs(certs_path, name, enforce=True)
            for ext in ['crt', 'key']:
                _copy_file(os.path.join(certs_path, f'{name}.{ext}'), f'./certs/{name}.{ext}')
        print(f"Copied {'default' if has_default else 'individual'} certificates to ./certs.")
        break

    return 'Default' if has_default else 'Individual'


CA_FILE = './certs/ca.pem'
CA_BUNDLE_FILE = './certs/bundle.pem'


def gather_ca_info():
    """Ask for extra CA certs to trust, e.g. a corporate CA for SSL inspection.

    Keeps a copy in ./certs/ca.pem so later runs keep it by default.
    Returns True if extra CA certs are in use.
    """
    has_existing = os.path.exists(CA_FILE)
    use_ca = ask_selection(
        "Does your network require trusting additional CA certificates?\n"
        "(e.g. a corporate CA, or SSL inspection of outbound traffic)",
        ['Yes', 'No'],
        default=1 if has_existing else 2,
    )
    if use_ca != '1':
        if has_existing:
            os.remove(CA_FILE)
        return False

    while True:
        hint = ' [keep existing]' if has_existing else ''
        ca_path = input(f"\nEnter path to CA certificate(s) PEM file{hint}: ")
        if not ca_path and has_existing:
            return True
        if not os.path.isfile(ca_path):
            print("File not found. Please try again.")
            continue
        with open(ca_path, 'r') as f:
            if '-----BEGIN CERTIFICATE-----' not in f.read():
                print("File must contain PEM certificates. Please try again.")
                continue
        os.makedirs('./certs', exist_ok=True)
        _copy_file(ca_path, CA_FILE)
        print(f"Copied CA certificates to {CA_FILE}.")
        return True


def write_ca_bundle(cert_files):
    """Write ./certs/bundle.pem: public CAs + extra CA + our TLS certs.

    Services use it to trust our own hosts and the corporate CA.
    """
    with open(CA_BUNDLE_FILE, 'w') as bundle_file:
        for path in [certifi.where(), *cert_files]:
            with open(path, 'r') as f:
                bundle_file.write(f.read().rstrip('\n') + '\n')


def create_inventory_file(inventory_file, mara_host_config):
    """Construct an Ansible inventory file with the given host configuration."""
    inventory = {
        'myhosts': {
            'hosts': {
                'mara-servers': mara_host_config,
            }
        }
    }

    # Define the path to save the inventory file
    # Write the YAML structure to the file
    with open(inventory_file, 'w') as file:
        yaml.dump(inventory, file, default_flow_style=False)
    print(f"Ansible inventory file has been created: {inventory_file}")
    return inventory_file


def collect_aws_credentials(existing_creds: dict = None):
    existing_creds = existing_creds or {}
    print("Please provide the AWS credentials given to you by Nanome:")
    access_key = existing_creds.get('aws_access_key', '')
    secret_key = existing_creds.get('aws_secret_key', '')
    masked_access_key = f"{'*' * len(access_key[:-4])}{access_key[-4:]}"
    masked_secret_key = f"{'*' * len(secret_key[:-4])}{secret_key[-4:]}"

    aws_access_key_id = input((
        f"Current AWS Access Key ID: {masked_access_key or 'None'}\n"
        f"New AWS Access Key ID: "
    ))

    aws_secret_access_key_id = getpass.getpass((
        f"Current AWS Secret Access Key: {masked_secret_key or 'None'}\n"
        f"New AWS Secret Access Key ID: "
    ))
    aws_region = 'us-west-1'
    credentials = {
        "aws_access_key": aws_access_key_id,
        "aws_secret_key": aws_secret_access_key_id,
        "region": aws_region
    }
    return credentials


def get_existing_aws_credentials():
    """Find existing AWS credentials configured on the server."""
    credentials = {}
    # Check environment variables
    access_key = os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")

    if access_key and secret_key:
        credentials["aws_access_key"] = access_key
        credentials["aws_secret_key"] = secret_key
        return credentials

    # Check AWS credentials file
    aws_credentials_path = os.path.expanduser("~/.aws/credentials")
    if os.path.exists(aws_credentials_path):
        try:
            with open(aws_credentials_path, "r") as cred_file:
                lines = cred_file.readlines()
                for line in lines:
                    if line.strip().startswith("aws_access_key_id"):
                        credentials["aws_access_key"] = line.split("=", 1)[1].strip()
                    if line.strip().startswith("aws_secret_access_key"):
                        credentials["aws_secret_key"] = line.split("=", 1)[1].strip()
                if "access_key" in credentials and "secret_key" in credentials:
                    return credentials
        except Exception as e:
            print(f"Error reading AWS credentials file: {e}")
    return credentials


def read_env_file(file_path):
    """Reads a .env file and converts its content to a dictionary.

    Args:
        file_path (str): The path to the .env file.

    Returns:
        dict: A dictionary containing key-value pairs from the .env file.
    """
    env_dict = {}

    if not os.path.exists(file_path):
        return env_dict

    with open(file_path, 'r') as file:
        for line in file:
            # Strip whitespace and skip empty lines or comments
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            # Split by the first `=` sign
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")  # Remove surrounding quotes if any
                env_dict[key] = value
    return env_dict


def write_env_file(env_filepath, env_dict: dict, append=False) -> None:
    """Write a dict to a .env file."""
    write_mode = 'a' if append else 'w'
    with open(env_filepath, write_mode) as f:
        for key, value in env_dict.items():
            line = f'{key}={value}\n'
            f.write(line)


def get_public_ip():
    """Get public ip from api.ipify.org."""
    ip_lookup_url = "https://api.ipify.org"
    try:
        with urllib.request.urlopen(ip_lookup_url) as response:
            return response.read().decode("utf-8")
    except Exception as e:
        print(f"Error fetching public IP: {e}")
        return None
