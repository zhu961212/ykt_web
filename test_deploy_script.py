import base64
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy.sh"


def _bash_executable():
    executable = shutil.which("bash")
    if executable:
        return executable
    if os.name == "nt":
        for environment_name in ("ProgramFiles", "ProgramFiles(x86)"):
            program_files = os.environ.get(environment_name)
            if not program_files:
                continue
            candidate = Path(program_files) / "Git" / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
    return None


class DeployScriptHttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    def section(self, start, end):
        start_index = self.script.index(start)
        end_index = self.script.index(end, start_index)
        return self.script[start_index:end_index]

    def validate_domain(self, value):
        bash = _bash_executable()
        if bash is None:
            self.skipTest("bash is unavailable")
        validator = self.section("validate_domain() {", "SCRIPT_PATH=")
        shell = (
            "set -e\n"
            "die() { printf '%s\\n' \"$*\" >&2; exit 64; }\n"
            + validator
            + "\nDOMAIN=\"$(printf '%s' \"$1\" | base64 --decode)\"\n"
            + "validate_domain\nprintf '%s\\n' \"$DOMAIN\"\n"
        )
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return subprocess.run(
            [bash, "-c", shell, "domain-validator", encoded],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )

    def run_caddy_package_installer(self, *, candidate, install_fails, installed=False):
        bash = _bash_executable()
        if bash is None:
            self.skipTest("bash is unavailable")
        functions = self.section("caddy_package_installed() {", "check_caddy_file() {")
        shell = (
            "TRACE=''\n"
            f"FAKE_CANDIDATE={int(candidate)}\n"
            f"FAKE_INSTALL_FAILS={int(install_fails)}\n"
            f"FAKE_INSTALLED={int(installed)}\n"
            "trace() { TRACE=\"${TRACE}${TRACE:+,}$*\"; }\n"
            "log() { :; }\n"
            "warn() { :; }\n"
            "caddy() { :; }\n"
            "dpkg-query() { (( FAKE_INSTALLED )) && printf 'install ok installed'; }\n"
            "apt-cache() { (( FAKE_CANDIDATE )) && printf '  Candidate: 2.10.2\\n' || printf '  Candidate: (none)\\n'; }\n"
            "configure_caddy_apt_repository() { trace repo; FAKE_CANDIDATE=1; }\n"
            "apt-get() {\n"
            "  trace \"apt-get:$*\"\n"
            "  if [[ \"$*\" == 'install -y caddy' ]]; then\n"
            "    (( FAKE_INSTALL_FAILS )) && return 1\n"
            "    FAKE_INSTALLED=1\n"
            "  fi\n"
            "  return 0\n"
            "}\n"
            + functions
            + "\nif install_caddy_package; then result=0; else result=$?; fi\n"
            + "printf 'result=%s\\ntrace=%s\\n' \"$result\" \"$TRACE\"\n"
        )
        return subprocess.run(
            [bash, "-c", shell], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10, check=False,
        )

    def run_caddy_repo_configurator(self, *, fail_dearmor):
        bash = _bash_executable()
        if bash is None:
            self.skipTest("bash is unavailable")
        function = self.section(
            "configure_caddy_apt_repository() (", "caddy_package_installed() {",
        )
        function = function.replace(
            "/usr/share/keyrings/caddy-stable-archive-keyring.gpg",
            "$TEST_ROOT/keyrings/caddy.gpg",
        ).replace(
            "/etc/apt/sources.list.d/caddy-stable.list",
            "$TEST_ROOT/sources/caddy.list",
        ).replace(
            "/usr/share/keyrings", "$TEST_ROOT/keyrings",
        ).replace(
            "/etc/apt/sources.list.d", "$TEST_ROOT/sources",
        )
        shell = (
            "if command -v cygpath >/dev/null 2>&1; then TEST_ROOT=\"$(cygpath -u \"$1\")\"; else TEST_ROOT=\"$1\"; fi\n"
            "ADMIN_DIR=\"$TEST_ROOT/admin\"\n"
            f"FAIL_DEARMOR={int(fail_dearmor)}\n"
            "mkdir -p \"$ADMIN_DIR\"\n"
            "warn() { :; }\n"
            "apt-get() { return 0; }\n"
            "write_output_arg() {\n"
            "  local previous='' argument output=''\n"
            "  for argument in \"$@\"; do\n"
            "    [[ \"$previous\" == '--output' ]] && output=\"$argument\"\n"
            "    previous=\"$argument\"\n"
            "  done\n"
            "  [[ -n \"$output\" ]] && printf 'data' > \"$output\"\n"
            "}\n"
            "curl() { write_output_arg \"$@\"; }\n"
            "gpg() {\n"
            "  if [[ \" $* \" == *' --show-keys '* ]]; then\n"
            "    printf 'pub:::::::::\\nfpr:::::::::65760C51EDEA2017CEA2CA15155B6D79CA56EA34:\\n'\n"
            "    return 0\n"
            "  fi\n"
            "  if [[ \" $* \" == *' --dearmor '* ]]; then\n"
            "    (( FAIL_DEARMOR )) && return 1\n"
            "    write_output_arg \"$@\"\n"
            "  fi\n"
            "}\n"
            + function
            + "\nif configure_caddy_apt_repository; then result=0; else result=$?; fi\n"
            + "key=$(test -e \"$TEST_ROOT/keyrings/caddy.gpg\" && echo yes || echo no)\n"
            + "source=$(test -e \"$TEST_ROOT/sources/caddy.list\" && echo yes || echo no)\n"
            + "printf 'result=%s\\nkey=%s\\nsource=%s\\n' \"$result\" \"$key\" \"$source\"\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            return subprocess.run(
                [bash, "-c", shell, "repo-configurator", directory],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10, check=False,
            )

    def test_executable_help_documents_domain_modes(self):
        bash = _bash_executable()
        if bash is None:
            self.skipTest("bash is unavailable")
        completed = subprocess.run(
            [bash, "scripts/deploy.sh", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--domain HOSTNAME", completed.stdout)
        self.assertIn("--no-domain", completed.stdout)

    def test_domain_validator_accepts_hosts_and_rejects_caddyfile_injection(self):
        for value, normalized in (
            ("Panel.Example.COM.", "panel.example.com"),
            ("scan.xn--fiqs8s.example", "scan.xn--fiqs8s.example"),
        ):
            with self.subTest(value=value):
                completed = self.validate_domain(value)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout.strip(), normalized)

        invalid = (
            "https://panel.example.com", "panel.example.com:443",
            "panel.example.com/path", "*.example.com", "127.0.0.1",
            "localhost", "bad_name.example.com", "bad..example.com",
            ("a" * 64) + ".example.com", "panel.example.123",
            "panel.example.com\nmalicious.example",
        )
        for value in invalid:
            with self.subTest(value=value):
                completed = self.validate_domain(value)
                self.assertNotEqual(completed.returncode, 0)

    def test_domain_is_loaded_and_persisted_in_deploy_metadata(self):
        self.assertRegex(
            self.script,
            r"\bDOMAIN\)\s+saved_domain=\"\$value\"\s+;;",
        )
        self.assertRegex(
            self.script,
            r"if \(\( domain_explicit == 0 \)\); then\s+DOMAIN=\"\$saved_domain\"\s+fi",
        )
        writer = self.section("write_deploy_config() {", "backup_admin_metadata() {")
        self.assertIn("printf 'DOMAIN=%s\\n' \"$DOMAIN\"", writer)

    def test_domain_mode_forces_loopback_and_secure_proxy_settings(self):
        settings = self.section(
            '    if [[ -n "$DOMAIN" ]]; then',
            "    if [[ -z \"$ADMIN_PASSWORD\" ]]; then",
        )
        self.assertIn('BIND_HOST="127.0.0.1"', settings)
        self.assertIn('SECURE_COOKIE="1"', settings)
        self.assertIn('TRUST_PROXY="1"', settings)

    def test_caddy_uses_an_independent_site_and_has_validation_and_rollback(self):
        self.assertIn('CADDY_SITE_DIR="/etc/caddy/ykt-web.d"', self.script)
        self.assertIn(
            'CADDY_SITE_FILE="$CADDY_SITE_DIR/$SERVICE_NAME.caddy"',
            self.script,
        )
        self.assertIn(
            'CADDY_IMPORT_LINE="import /etc/caddy/ykt-web.d/*.caddy"',
            self.script,
        )
        activation = self.section("activate_https_proxy() {", "if [[ \"$BIND_HOST\"")
        self.assertRegex(
            activation,
            r'caddy validate --config "\$CADDY_MAIN_CONFIG" --adapter caddyfile',
        )
        self.assertIn("reverse_proxy 127.0.0.1:$BIND_PORT", activation)
        self.assertIn("restore_https_proxy() {", self.script)
        self.assertRegex(
            self.script,
            r"trap '[^']*restore_https_proxy[^']*' EXIT",
        )
        rollback = self.section("rollback_release() {", "activate_systemd_unit ||")
        self.assertIn("restore_https_proxy", rollback)
        same_release = self.section(
            'if (( FORCE_DEPLOY == 0 )) && [[ "$CURRENT_COMMIT" == "$TARGET_COMMIT" ]]',
            'release_stamp="$(date -u',
        )
        self.assertIn("restore_https_proxy", same_release)

    def test_apt_fallback_uses_pinned_official_caddy_repository(self):
        repository = self.section(
            "configure_caddy_apt_repository() (", "install_caddy_package() {",
        )
        installer = self.section("install_caddy_package() {", "check_caddy_file() {")
        self.assertIn(
            "65760C51EDEA2017CEA2CA15155B6D79CA56EA34", repository,
        )
        self.assertIn(
            "https://dl.cloudsmith.io/public/caddy/stable/gpg.key", repository,
        )
        self.assertIn(
            "https://dl.cloudsmith.io/public/caddy/stable/deb/debian", repository,
        )
        self.assertIn("--show-keys --with-colons", repository)
        self.assertIn("--dearmor", repository)
        self.assertIn("--no-options --homedir", repository)
        self.assertIn('public_key_count" != "1"', repository)
        self.assertIn("--proto '=https' --tlsv1.2", repository)
        self.assertNotRegex(repository, r"curl[^\n]*\|\s*(bash|sh)\b")
        self.assertIn("configure_caddy_apt_repository", installer)
        self.assertIn("apt-cache policy caddy", installer)
        self.assertIn("caddy_package_installed", repository)
        self.assertIn("dpkg-query -W", repository)
        self.assertEqual(installer.count("apt-get install -y caddy"), 1)

    def test_apt_fallback_only_runs_when_no_package_candidate_exists(self):
        missing = self.run_caddy_package_installer(
            candidate=False, install_fails=False,
        )
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertIn("result=0", missing.stdout)
        self.assertIn(
            "trace=apt-get:update,repo,apt-get:update,apt-get:install -y caddy",
            missing.stdout,
        )

        broken = self.run_caddy_package_installer(
            candidate=True, install_fails=True,
        )
        self.assertEqual(broken.returncode, 0, broken.stderr)
        self.assertIn("result=1", broken.stdout)
        self.assertNotIn("repo", broken.stdout)

        complete = self.run_caddy_package_installer(
            candidate=True, install_fails=False, installed=True,
        )
        self.assertEqual(complete.returncode, 0, complete.stderr)
        self.assertIn("result=0", complete.stdout)
        self.assertIn("trace=\n", complete.stdout)

    def test_repository_setup_stops_when_key_dearmor_fails(self):
        completed = self.run_caddy_repo_configurator(fail_dearmor=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("result=1", completed.stdout)
        self.assertIn("key=no", completed.stdout)
        self.assertIn("source=no", completed.stdout)

    def test_caddyfile_metadata_and_failed_rollback_backup_are_preserved(self):
        restore = self.section("restore_https_proxy() {", "finalize_https_proxy() {")
        activation = self.section("activate_https_proxy() {", "if [[ \"$BIND_HOST\"")
        self.assertIn('cp -a -- "$CADDY_BACKUP_DIR/Caddyfile"', restore)
        self.assertIn('cp -a -- "$CADDY_MAIN_CONFIG" "$main_next"', activation)
        self.assertIn('cp -a -- "$main_next" "$CADDY_MAIN_CONFIG.next.$$"', activation)
        self.assertIn("backup retained at $CADDY_BACKUP_DIR", restore)
        failed_restore = restore.index("if (( result )); then")
        backup_removal = restore.index('rm -rf -- "$CADDY_BACKUP_DIR"')
        self.assertLess(failed_restore, backup_removal)

    def test_no_domain_does_not_enable_or_start_caddy(self):
        activation = self.section("activate_https_proxy() {", "if [[ \"$BIND_HOST\"")
        disabled_start = activation.index('if [[ -z "$DOMAIN" ]]; then', 1)
        enable = activation.index("systemctl enable caddy", disabled_start)
        disabled_block = activation[disabled_start:enable]
        self.assertIn("CADDY_WAS_ACTIVE", disabled_block)
        self.assertIn("systemctl reload caddy", disabled_block)
        self.assertIn("return 0", disabled_block)
        self.assertNotIn("systemctl start caddy", disabled_block)

    def test_domain_mode_prints_https_access_url(self):
        output = self.section("print_access_details() {", 'if [[ -n "$CURRENT_COMMIT"')
        self.assertIn('log "Access URL: https://$DOMAIN/"', output)
        self.assertIn('log "Access URL: http://$access_host:$BIND_PORT/"', output)

    def test_explicit_domain_change_bypasses_same_commit_update_exit(self):
        no_update = self.section(
            "if (( UPDATE_MODE && FORCE_DEPLOY == 0",
            "if (( FORCE_DEPLOY == 0 ))",
        )
        self.assertIn("domain_explicit == 0", no_update)
        self.assertIn('[[ "$CURRENT_COMMIT" == "$TARGET_COMMIT" ]]', no_update)
        self.assertIn("exit 0", no_update)

    def test_no_domain_keeps_direct_http_defaults_and_skips_proxy(self):
        self.assertIn('DEFAULT_BIND_HOST="0.0.0.0"', self.script)
        self.assertIn('DEFAULT_BIND_PORT="8765"', self.script)
        self.assertIn('DEFAULT_SECURE_COOKIE="0"', self.script)
        self.assertIn('DEFAULT_TRUST_PROXY="0"', self.script)
        activation = self.section("activate_https_proxy() {", "set_caddy_paths")
        self.assertRegex(
            activation,
            re.compile(
                r'if \[\[ -z "\$DOMAIN" && \$DISABLE_DOMAIN -eq 0 \]\]; then\s+'
                r"return 0\s+fi",
                re.MULTILINE,
            ),
        )
        disabled = self.section("    elif (( DISABLE_DOMAIN )); then", "    fi\n    if [[ -z")
        self.assertIn('BIND_HOST="$DEFAULT_BIND_HOST"', disabled)
        self.assertIn('SECURE_COOKIE="$DEFAULT_SECURE_COOKIE"', disabled)
        self.assertIn('TRUST_PROXY="$DEFAULT_TRUST_PROXY"', disabled)


if __name__ == "__main__":
    unittest.main()
