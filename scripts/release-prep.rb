#!/usr/bin/env ruby
# frozen_string_literal: true

# release-prep.rb — generates the Homebrew formula, the Homebrew cask and the
# MacPorts Portfile for a NanoDictate release.
#
# Usage:  ruby scripts/release-prep.rb v0.0.3
#
# Steps:
#   1. downloads the four binary assets attached to the GitHub Release — the
#      two tarballs (formula + MacPorts port) and the two app-bundle zips
#      (cask):
#      .../download/<tag>/nanodictate-<ver>-macos-{arm64,x86_64}.{tar.gz,zip}
#   2. computes sha256 for all four (the formula and the port pin the tarball
#      hashes, the cask pins the zip hashes)
#   3. substitutes __VERSION__, __SHA256_ARM64__, __SHA256_X86_64__,
#      __ZIP_SHA256_ARM64__, __ZIP_SHA256_X86_64__, __MAINTAINERS__ (env
#      MAINTAINERS), __REVISION__ (env REVISION, default 0) in the .tpl files
#   4. writes packaging/homebrew/nanodictate.rb,
#      packaging/homebrew/Casks/nanodictate.rb and packaging/macports/Portfile
#
# Stdlib only (open-uri, digest, fileutils). No external gems.
#
# NOTE: this needs the GitHub Release to exist (with all four assets
# attached), so it runs only after the release workflow has published a tag.

require "open-uri"
require "digest/sha2"
require "uri"
require "fileutils"

TAG = ARGV[0]
unless TAG && TAG =~ /\Av?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\z/
  abort "Usage: ruby scripts/release-prep.rb v0.0.3   (tag may be '0.0.3' or 'v0.0.3')"
end

VERSION     = TAG.sub(/\Av/, "")
ROOT = File.expand_path("..", __dir__)

# Binary tarballs attached to the GitHub Release. The tag in the download path
# keeps the "v" prefix; the archive filename does not. Contract fixed in
# .github/workflows/release.yml — keep this in sync with it.
def binary_url(arch)
  "https://github.com/kodmial/nanodictate/releases/download/#{TAG}/" \
    "nanodictate-#{VERSION}-macos-#{arch}.tar.gz"
end

# App-bundle zips attached to the GitHub Release — the cask asset. Same
# naming contract as the tarballs; distinct asset, consumed only by the cask.
def cask_url(arch)
  "https://github.com/kodmial/nanodictate/releases/download/#{TAG}/" \
    "nanodictate-#{VERSION}-macos-#{arch}.zip"
end

def filled_template(path, values)
  content = File.read(path)
  values.each { |k, v| content = content.gsub(k, v) }
  content
end

def network_blocked_msg(err)
  "Download blocked: #{err.class}: #{err.message}\n" \
    "  Direct GitHub access appears blocked; retry from a network where\n" \
    "  github.com is reachable.\n"
end

# Download +url+, aborting with a tag-specific message on failure. Exits the
# script, so a non-nil return is guaranteed on success.
def download(url, what)
  puts "Downloading #{url}"
  URI.open(url, read_timeout: 120, open_timeout: 30).read
rescue OpenURI::HTTPError => e
  abort "Download failed: HTTP #{e.io.status.join(" ")} — is tag '#{TAG}' published?\n" \
        "  Verify in a browser first that the #{what} exists, then re-run.\n"
rescue SystemCallError, SocketError, Timeout::Error => e
  abort network_blocked_msg(e)
rescue EOFError => e
  # Connection-level failures, e.g. a dropped connection mid-download.
  # Anything else (a script bug, an unexpected error class) is left to
  # propagate — aborting here would mislabel it as a download issue.
  abort network_blocked_msg(e)
end

# --- 0. Environment (fail fast before any download) --------------------------
# The MacPorts maintainers handle is a person's id — never guessed, always
# passed in. CI sends it via the MAINTAINERS env var (release.yml); locally:
#   MAINTAINERS=@kodmial ruby scripts/release-prep.rb v0.0.3
# An unset/empty value aborts: a silently empty maintainers line would break
# the port and is not caught by the placeholder sanity check in CI.
maintainers = ENV["MAINTAINERS"]
if maintainers.nil? || maintainers.strip.empty?
  abort "MAINTAINERS is not set (or empty) — pass the MacPorts maintainers handle, " \
        "e.g. MAINTAINERS=@kodmial ruby scripts/release-prep.rb #{TAG}"
end

# revision directive for the Portfile: 0 for a first publish of this version;
# bump via REVISION env on a re-release without a version change.
revision = ENV["REVISION"]
revision = "0" if revision.nil? || revision.strip.empty?

# --- 1. Download all four binary assets --------------------------------------
arm = download(binary_url("arm64"), "arm64 binary tarball")
x86 = download(binary_url("x86_64"), "x86_64 binary tarball")
zarm = download(cask_url("arm64"), "arm64 app-bundle zip")
zx86 = download(cask_url("x86_64"), "x86_64 app-bundle zip")

# --- 2. Checksums --------------------------------------------------------------
# Tarballs: Homebrew formula + MacPorts Portfile.
sha256_arm64  = Digest::SHA256.hexdigest(arm)
sha256_x86_64 = Digest::SHA256.hexdigest(x86)
# App-bundle zips: Homebrew cask ONLY — the zip placeholders are distinct from
# the tarball tokens so the checksums can never cross-contaminate.
zip_sha256_arm64  = Digest::SHA256.hexdigest(zarm)
zip_sha256_x86_64 = Digest::SHA256.hexdigest(zx86)

# --- 3. Fill templates -------------------------------------------------------
values = {
  "__VERSION__"          => VERSION,                 # "0.0.3" (no leading v)
  "__SHA256_ARM64__"     => sha256_arm64,            # Homebrew formula + Portfile, arm64 tarball
  "__SHA256_X86_64__"    => sha256_x86_64,           # Homebrew formula + Portfile, x86_64 tarball
  "__ZIP_SHA256_ARM64__" => zip_sha256_arm64,        # Homebrew cask, arm64 app-bundle zip
  "__ZIP_SHA256_X86_64__" => zip_sha256_x86_64,      # Homebrew cask, x86_64 app-bundle zip
  "__MAINTAINERS__"      => maintainers,             # Portfile, MacPorts maintainers handle
  "__REVISION__"         => revision,                # Portfile revision directive (default "0")
}

formula_tpl  = File.join(ROOT, "packaging", "homebrew", "nanodictate.rb.tpl")
formula_out  = File.join(ROOT, "packaging", "homebrew", "nanodictate.rb")
cask_tpl     = File.join(ROOT, "packaging", "homebrew", "Casks", "nanodictate.rb.tpl")
cask_out     = File.join(ROOT, "packaging", "homebrew", "Casks", "nanodictate.rb")
portfile_tpl = File.join(ROOT, "packaging", "macports", "Portfile.tpl")
portfile_out = File.join(ROOT, "packaging", "macports", "Portfile")

FileUtils.mkdir_p(File.dirname(cask_out))
File.write(formula_out, filled_template(formula_tpl, values))
File.write(cask_out, filled_template(cask_tpl, values))
File.write(portfile_out, filled_template(portfile_tpl, values))

# --- 4. Report and instructions ----------------------------------------------
puts "Wrote:"
puts "  #{formula_out}"
puts "  #{cask_out}"
puts "  #{portfile_out}"
puts "Checksums for #{TAG}:"
puts "  arm64 binary tarball   sha256  #{sha256_arm64}"
puts "  x86_64 binary tarball  sha256  #{sha256_x86_64}"
puts "  arm64 app-bundle zip   sha256  #{zip_sha256_arm64}"
puts "  x86_64 app-bundle zip  sha256  #{zip_sha256_x86_64}"
puts
puts "Next steps:"
puts "  1. Verify the maintainers line in #{portfile_out} (filled from the MAINTAINERS env var)."
puts "  2. Sanity-check the version string: `nanodictate --version` will print \"nanodictate #{VERSION}\"."
puts "  3. Homebrew: copy #{formula_out} into the kodmial/homebrew-nanodictate tap as"
puts "     nanodictate.rb (repo root) and #{cask_out} as Casks/nanodictate.rb — on"
puts "     CI the release workflow's manifests job does this commit + tap copy"
puts "     automatically; then run `brew audit --strict --new nanodictate` there."
puts "  4. MacPorts: copy #{portfile_out} into a macports-ports checkout as"
puts "     audio/nanodictate/Portfile, then run `port lint` before opening the PR."
puts "  5. Test a real install: `brew install nanodictate`, `brew install --cask nanodictate`"
puts "     and `port install`."