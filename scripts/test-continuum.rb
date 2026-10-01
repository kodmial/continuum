# frozen_string_literal: true

require 'minitest/autorun'
require 'yaml'
require 'tmpdir'
require 'fileutils'
require 'open3'

class ContinuumTest < Minitest::Test
  ROOT = File.expand_path('..', __dir__)
  STUBS = (Dir[File.join(ROOT, '.github/caller-stubs/*.yml')] +
           Dir[File.join(ROOT, '.github/caller-stubs/tech/*.yml')]).sort
  TECH_STUBS = Dir[File.join(ROOT, '.github/caller-stubs/tech/*.yml')].sort
  CORE_STUBS = Dir[File.join(ROOT, '.github/caller-stubs/*.yml')].sort
  PARENT_STUBS = Dir[File.join(ROOT, '.github/caller-stubs/parent/*.yml')].sort
  WORKFLOWS = Dir[File.join(ROOT, '.github/workflows/*.yml')].sort
  # Filenames that predate the `continuum-` prefix and are part of the shipped
  # interface. They are core-layer workflows; the prefix was never applied to
  # them and renaming them would break every installed consumer.
  HISTORICAL_UNPREFIXED = %w[
    add-review-label.yml
    auto-merge.yml
    bootstrap-runtime-secret.yml
    coderabbit-retry.yml
    coderabbit-unresolved.yml
    consumer-child-dispatcher.yml
    consumer-child-pr-review.yml
    consumer-child-review.yml
    consumer-child-worker.yml
    issue-scheduler.yml
    opencode-repair.yml
    opencode-unresolved.yml
    opencode.yml
    pr-agent.yml
    remove-review-label.yml
    validate-continuum.yml
  ].freeze

  def yaml(path)
    YAML.load_file(path)
  end

  def workflow_body(name)
    File.read(File.join(ROOT, '.github/workflows', name))
  end

  # Only the embedded `script: |` block. Assertions about *engine* literals must
  # not see the `workflow_call.inputs` defaults, which legitimately name the
  # same values as the fallback chain.
  def script_body(name)
    lines = workflow_body(name).lines
    start = lines.index { |line| line =~ /^\s*script: \|\s*$/ }
    refute_nil start, "#{name}: no embedded script block"
    indent = lines[start][/\A */].size + 2
    rest = lines[(start + 1)..]
    stop = rest.index { |line| !line.strip.empty? && line[/\A */].size < indent }
    block = stop ? rest[0...stop] : rest
    block.map { |line| line[/\A */].size >= indent ? line[indent..] : line }.join
  end

  # The parsed core workflow behind a caller stub of the same base name.
  def workflow_stub_callee
    yaml(File.join(ROOT, '.github/workflows', 'opencode-repair.yml'))
  end

  # The modes the OpenCode engine admits on the `workflow_dispatch` path,
  # parsed out of the job's own `if` guard rather than hardcoded here.
  def dispatch_modes
    body = workflow_body('opencode.yml')
    body[/contains\(fromJSON\('\[([^\]]+)\]'\), inputs\.mode\)/, 1].to_s
        .scan(/"([^"]+)"/).flatten
  end

  # Every `.../dispatches` call site, captured together with the lines that
  # follow it, so a test can prove the call is really made and really carries
  # the payload. A call replaced by an `echo` leaves no window at all, which is
  # exactly the "green no-op" a presence-only check misses.
  DISPATCHING_WORKFLOWS = %w[
    auto-merge.yml
    opencode-repair.yml
    opencode-unresolved.yml
    opencode.yml
  ].freeze

  def dispatch_calls(name)
    lines = workflow_body(name).lines
    lines.each_index.select { |index| lines[index].include?('/dispatches') }
         .map { |index| lines[[index - 2, 0].max, 12].join }
  end

  def all_dispatch_calls
    DISPATCHING_WORKFLOWS.flat_map { |name| dispatch_calls(name) }
  end

  def events(workflow)
    workflow['on'] || workflow[true]
  end

  # The `continuum-tech-<tech>-` prefix marks a technology-library workflow.
  # The `<name>` part may itself be multi-word, so the rule is a `tech`
  # segment plus at least two further segments — not a fixed dash count.
  def tech_name?(base)
    segments = base.delete_suffix('.yml').split('-')
    base.start_with?('continuum-') && segments[1] == 'tech' && segments.size >= 4
  end

  # The technology layer exists as a pair: a caller stub and the reusable
  # workflow it calls. The two halves must name exactly the same set of files.
  # Neither half is pinned to a literal count, so adding a technology (or a
  # tech workflow) needs no test edit — but dropping a stub leaves its callee
  # orphaned and fails here.
  def assert_tech_layers_agree
    stubs = TECH_STUBS.map { |path| File.basename(path) }.sort
    workflows = WORKFLOWS.map { |path| File.basename(path) }.select { |base| tech_name?(base) }.sort
    assert_equal stubs, workflows, 'tech stubs and tech workflows must be the same set of files'
  end

  # Installed core callers must carry exactly the `continuum-` prefix on their
  # own stub name: no more, which would let a core caller be mistaken for a
  # tech-library caller, and no less, which would make a Continuum caller look
  # project-owned and stop Continuum's dispatchers from finding it.
  def assert_core_install_names(dir)
    installed = Dir[File.join(dir, '.github/workflows/*.yml')].map { |file| File.basename(file) }.sort
    # Mirrors install.sh: an already `continuum-`-prefixed stub (the
    # `continuum-opencode-watchdog` one) keeps its name, every other core stub
    # gains the prefix.
    expected = CORE_STUBS.map { |path|
      base = File.basename(path)
      base.start_with?('continuum-') ? base : "continuum-#{base}"
    }.sort
    assert_equal expected, installed
    installed.each do |name|
      assert name.start_with?('continuum-'), name
      refute name.start_with?('continuum-tech-'), name
    end
  end

  # The `if (` openers that genuinely enclose the line at `index`, returned
  # innermost-first. A brace-balance walk is unreliable in JavaScript embedded
  # in YAML (template literals, braces inside strings), but the indentation of
  # these scripts is exact, so the block structure can be recovered by walking
  # upward and tracking the current level: a strictly-less-indented line is the
  # head of the enclosing block, and an `if (` head is one of its gates.
  #
  # Taking merely the last few `if (` lines above the target is NOT equivalent:
  # those are often already-closed siblings, which is how an ungated write can
  # look gated.
  def enclosing_gates(lines, index)
    level = lines[index][/\A */].size
    gates = []
    (index - 1).downto(0) do |position|
      line = lines[position]
      next if line.strip.empty?
      indent = line[/\A */].size
      next if indent > level
      gates << line if line =~ /^\s*if \(/
      level = indent
    end
    gates.reverse
  end

  def each_pair
    STUBS.each do |path|
      callee_path = File.join(ROOT, '.github/workflows', File.basename(path))
      yield yaml(path), yaml(callee_path), path, callee_path
    end
  end

  # Every sibling-workflow reference a workflow file makes, as "<file>:<line>".
  # Only local references count: `kodmial/continuum/...@ref` always points at
  # this repository's own flat filenames and is not renamed on installation.
  def referenced_workflow_paths(workflow, path)
    File.readlines(path).each_with_index.each_with_object([]) do |(line, index), found|
      next unless line.match?(%r{\.github/workflows/|workflow_id: '|\w+_workflow:})
      next if line.include?('kodmial/continuum/')
      names = line.scan(%r{\.github/workflows/([A-Za-z0-9._/-]+\.yml)}).flatten +
              line.scan(/workflow_id: '([A-Za-z0-9._-]+\.yml)'/).flatten +
              # `<name>_workflow: <file>.yml` — a bare filename handed to
              # `gh workflow run`, which resolves by path, not by `name:`.
              # An interpolated value is not a sibling reference.
              line.scan(/(?:\w+_workflow|WORKER_WORKFLOW|REVIEW_WORKFLOW|MANUAL_PR_REVIEW_WORKFLOW):\s*'?([A-Za-z0-9._-]+\.yml)'?(?!\s*\$)/).flatten
      unprefixed = names.reject { |name| name.start_with?('continuum-') }
      found << "#{File.basename(path)}:#{index + 1} #{unprefixed.join(', ')}" unless unprefixed.empty?
    end
  end

  def fixture
    parent = File.join(ROOT, '.opencode-tmp')
    FileUtils.mkdir_p(parent)
    Dir.mktmpdir('continuum-tests-', parent) { |dir| yield dir }
  ensure
    Dir.rmdir(parent) if parent && Dir.exist?(parent) && Dir.empty?(parent)
  end

  def test_callable_contracts_and_dispatch_inputs
    each_pair do |caller, callee|
      assert_equal ['workflow_call'], events(callee).keys
      call = events(callee).fetch('workflow_call')
      assert(call['secrets'].nil? || call['secrets'].is_a?(Hash))
      assert_equal callee['name'], caller['name']
      job = caller.fetch('jobs').fetch('call')
      events(caller).fetch('workflow_dispatch', nil).to_h.fetch('inputs', {}).each do |key, value|
        expected = value['type'] == 'choice' ? 'string' : value['type']
        assert_equal expected, call.fetch('inputs').fetch(key).fetch('type')
        assert_includes job.fetch('with').fetch(key), "inputs.#{key}"
      end
      job.fetch('with').each_key { |key| assert call.fetch('inputs').key?(key) }
      assert_equal 'main', job.fetch('with').fetch('continuum_ref')
      assert_equal 'inherit', job['secrets']
    end
  end

  def test_callers_grant_required_permissions
    rank = {'none'=>0, 'read'=>1, 'write'=>2}
    each_pair do |caller, callee|
      permissions = caller.fetch('permissions')
      ([callee['permissions']] + callee['jobs'].values.map { |job| job['permissions'] }).compact.each do |required|
        required.each do |key, value|
          assert_operator rank.fetch(permissions.fetch(key, 'none')), :>=, rank.fetch(value), "#{caller['name']}: #{key}"
        end
      end
    end
  end

  def test_workflow_run_dependencies_exist
    names = STUBS.map { |path| yaml(path).fetch('name') }
    each_pair do |caller, _|
      events(caller).fetch('workflow_run', {}).fetch('workflows', []).each do |name|
        assert_includes names, name
      end
    end
  end

  # `workflow_run.workflows` matches a workflow's `name:` VALUE, so two
  # workflows sharing a `name:` are indistinguishable to the filter. The
  # watchdog is the dangerous case: give the core watchdog and its stub the
  # OpenCode caller's `name:` and it listens for its own completed runs, which
  # dispatches another recovery, which completes, which triggers the watchdog
  # again — an infinite self-trigger loop that no other assertion catches.
  #
  # The two sets are disjoint: a core workflow's `name:` is the name of the
  # INSTALLED stub that calls it, and Continuum's own repository runs the core
  # files directly, so `OpenCode agent` legitimately appears in both sets.
  # Uniqueness is therefore required *within* each set, not across them.
  def test_workflow_names_are_unique_within_each_layer
    {
      'core workflows' => WORKFLOWS,
      'core stubs' => CORE_STUBS,
      'tech stubs' => TECH_STUBS,
      'parent stubs' => PARENT_STUBS
    }.each do |layer, paths|
      names = paths.map { |path| yaml(path).fetch('name') }
      assert_equal names, names.uniq,
                   "#{layer}: two workflows share a `name:`, which makes a " \
                   'workflow_run.workflows filter ambiguous'
    end
  end

  # A workflow must never watch its own `name:`. Within a layer the names are
  # unique, so a self-reference can only be an explicit copy of the file's own
  # name into its filter — the exact shape of the watchdog self-trigger.
  def test_no_workflow_run_trigger_watches_itself
    watched_sources = WORKFLOWS + CORE_STUBS + TECH_STUBS + PARENT_STUBS
    watched_sources.each do |path|
      caller = yaml(path)
      watched = events(caller).fetch('workflow_run', {}).fetch('workflows', [])
      watched.each do |name|
        refute_equal caller.fetch('name'), name,
                     "#{File.basename(path)}: workflow_run watches its own `name:` " \
                     '(infinite self-trigger)'
      end
    end

    # Specifically: the watchdog exists to recover failed OpenCode runs, so
    # nothing may name the watchdog in a `workflow_run` filter — least of all
    # the watchdog itself.
    watchdog_name = yaml(File.join(ROOT, '.github/workflows', WATCHDOG)).fetch('name')
    watched_sources.each do |path|
      watched = events(yaml(path)).fetch('workflow_run', {}).fetch('workflows', [])
      watched.each do |name|
        refute_equal watchdog_name, name,
                     "#{File.basename(path)}: watches the OpenCode watchdog, " \
                     'so the watchdog recovers itself'
      end
    end
  end

  # A stub is written into a consumer repository under its `continuum-` name,
  # so every reference it makes to a sibling workflow must carry the same
  # prefix. `paths-ignore` filters and `workflow_id` dispatch targets are
  # resolved by file path, not by `name:`, so an unprefixed reference would
  # silently stop matching after installation.
  def test_stubs_only_reference_prefixed_sibling_workflows
    each_pair do |caller, callee, caller_path, callee_path|
      assert_empty referenced_workflow_paths(caller, caller_path), caller['name']
      assert_empty referenced_workflow_paths(callee, callee_path), callee['name']
    end
    Dir[File.join(ROOT, '.github/caller-stubs/parent/*.yml')].each do |path|
      yaml(path).fetch('jobs').each_value do |job|
        assert_empty referenced_workflow_paths(job, path), "#{File.basename(path)}:#{job['uses']}"
      end
    end
  end

  # Continuum ships two layers and the split is machine-checkable through the
  # file name:
  #
  #   1. core   — `continuum-<name>.yml`, at least two dash-separated segments
  #               and never a `continuum-tech-…` name, e.g.
  #               `continuum-auto-merge.yml` or `continuum-opencode-watchdog.yml`.
  #               Installed by the default `core` set; every project needs these.
  #   2. tech   — `continuum-tech-<tech>-<name>.yml`, marked by the
  #               `continuum-tech-<tech>-` prefix, e.g.
  #               `continuum-tech-swift-release.yml`. Installed only by the
  #               opt-in `tech` set; Continuum never triggers these itself.
  #
  # A third, legitimate category exists: files whose names predate the prefix
  # (`add-review-label.yml`, `consumer-child-*.yml`, `validate-continuum.yml`).
  # They are core-layer workflows shipped under their historical names; the
  # prefix was never applied to them and renaming them would break every
  # installed consumer, so the split must not "fix" them.
  def test_workflow_files_obey_the_two_layer_naming_rule
    covered = HISTORICAL_UNPREFIXED
    WORKFLOWS.each do |path|
      base = File.basename(path)
      segments = base.delete_suffix('.yml').split('-')
      category =
        if covered.include?(base)
          :historical_unprefixed_core
        elsif tech_name?(base)
          :tech
        # A core name may itself be multi-word (`continuum-opencode-watchdog`),
        # so the split counts on the `continuum-tech-<tech>-` marker rather
        # than on a segment count.
        elsif base.start_with?('continuum-') && segments.size >= 2
          :core
        else
          flunk "#{base}: neither a core file (continuum-<name>.yml, `continuum-` prefixed), " \
                'a tech file (continuum-tech-<tech>-<name>.yml, continuum-tech-<tech>- prefix), ' \
                'nor a listed historical unprefixed core name'
        end
      assert_includes %i[core tech historical_unprefixed_core], category
    end
    unprefixed = WORKFLOWS.map { |path| File.basename(path) }.reject { |base| base.start_with?('continuum-') }
    assert_equal unprefixed.sort, covered.sort, 'the historical unprefixed core list is stale'
    assert_tech_layers_agree
  end

  # The `continuum-tech-` prefix means "opt-in library". A tech workflow that any
  # other trigger could fire on would let Continuum trigger its own technology
  # library, which is exactly what the split exists to prevent.
  def test_tech_workflows_are_only_reusable_and_never_self_triggered
    assert_tech_layers_agree
    TECH_STUBS.each do |stub_path|
      callee_path = File.join(ROOT, '.github/workflows', File.basename(stub_path))
      assert File.file?(callee_path), "missing callee for #{File.basename(stub_path)}"
      assert_equal ['workflow_call'], events(yaml(callee_path)).keys, File.basename(callee_path)
    end
    # `workflow_run`/`push`/`schedule` would let a core workflow cascade into
    # the technology library; only `workflow_call` keeps it opt-in.
    WORKFLOWS.each do |path|
      base = File.basename(path)
      next unless tech_name?(base)
      assert_equal ['workflow_call'], events(yaml(path)).keys, base
    end
  end

  def test_manifest_environment_belongs_to_executable_step
    release = yaml(File.join(ROOT, '.github/workflows/continuum-tech-swift-release.yml'))
    steps = release.fetch('jobs').fetch('manifests').fetch('steps')
    steps.each { |step| assert(step.key?('run') || step.key?('uses'), step['name']) }
    generate = steps.find { |step| step['run'].to_s.include?('ruby scripts/release-prep.rb "v$VERSION"') }
    %w[VERSION MAINTAINERS REVISION].each { |key| assert generate.fetch('env').key?(key) }
  end

  def test_release_candidate_gate_normalizes_reusable_job_names
    release = yaml(File.join(ROOT, '.github/workflows/continuum-tech-swift-release.yml'))
    gate = release.fetch('jobs').fetch('candidate-gate').fetch('steps')
                  .find { |step| step['name'] == 'Wait for exact-head Packaging smoke' }
    run = gate.fetch('run')

    assert_includes run, %q{--jq '.jobs[].name | split(" / ") | last'}
    assert_includes run, "grep -qxF 'Candidate build (x86_64)'"
    assert_includes run, "grep -qxF 'Candidate build (arm64)'"
  end

  def test_fallback_preserves_existing_scripts_and_copies_missing_files
    fixture do |dir|
      FileUtils.mkdir_p(File.join(dir, 'scripts'))
      File.write(File.join(dir, 'scripts/local.sh'), 'consumer')
      File.write(File.join(dir, 'scripts/release-policy.sh'), 'consumer policy')
      FileUtils.mkdir_p(File.join(dir, '.continuum'))
      FileUtils.cp_r(File.join(ROOT, 'scripts'), File.join(dir, '.continuum/scripts'))
      each_pair do |_, callee|
        callee['jobs'].each_value do |job|
          job.fetch('steps', []).each do |step|
            next unless step['name'] == 'Copy missing Continuum scripts'
            output, status = Open3.capture2e('bash', '-e', '-c', step.fetch('run'), chdir: dir)
            assert status.success?, output
          end
        end
      end
      assert_equal 'consumer', File.read(File.join(dir, 'scripts/local.sh'))
      assert_equal 'consumer policy', File.read(File.join(dir, 'scripts/release-policy.sh'))
      %w[release-prep.rb test-release-policy.sh packaging-smoke/common.sh packaging-smoke/make-candidate.sh].each do |file|
        assert File.file?(File.join(dir, 'scripts', file)), file
      end
    end
  end

  # Measured counts, not guesses: `core` installs every stub directly under
  # `.github/caller-stubs/` (task-domain layer), `tech` the
  # `continuum-tech-<tech>-*` stubs in `.github/caller-stubs/tech/`, and
  # `parent` the child-execution stubs in `.github/caller-stubs/parent/`.
  # The tech count is derived from the stub set, so a second technology does
  # not need a test edit and a dropped stub still fails.
  CORE_COUNT = 13
  TECH_COUNT = TECH_STUBS.size
  PARENT_COUNT = 4

  def test_installer_local_and_explicit_ref
    fixture do |dir|
      assert_equal CORE_COUNT, CORE_STUBS.size, 'core stub set drifted'
      output, status = Open3.capture2e('bash', File.join(ROOT, 'install.sh'), File.join(dir, 'local'))
      assert status.success?, output
      assert_equal CORE_COUNT, Dir[File.join(dir, 'local/.github/workflows/*.yml')].size
      assert_core_install_names(File.join(dir, 'local'))
      bin = File.join(dir, 'bin')
      FileUtils.mkdir_p(bin)
      # Record downloads and serve templates locally, without network requests.
      File.write(File.join(bin, 'curl'), <<~SH)
        #!/usr/bin/env bash
        set -eu
        printf '%s\\n' "$*" >> "$DOWNLOAD_LOG"
        url="${@: -1}"
        cat "$TEMPLATES/${url#*caller-stubs/}"
      SH
      FileUtils.chmod(0755, File.join(bin, 'curl'))
      env = {'PATH'=>"#{bin}:#{ENV['PATH']}", 'DOWNLOAD_LOG'=>File.join(dir, 'downloads'), 'TEMPLATES'=>File.join(ROOT, '.github/caller-stubs')}
      ['release/v2', 'a' * 40, '123'].each do |ref|
        output, status = Open3.capture2e(env, 'bash', File.join(ROOT, 'install.sh'), File.join(dir, 'pinned'), ref)
        assert status.success?, output
        assert_core_install_names(File.join(dir, 'pinned'))
        Dir[File.join(dir, 'pinned/.github/workflows/*.yml')].each do |file|
          job = yaml(file).fetch('jobs').fetch('call')
          assert job['uses'].end_with?("@#{ref}")
          assert_equal ref, job.fetch('with').fetch('continuum_ref')
        end
        assert_includes File.read(env['DOWNLOAD_LOG']), "/#{ref}/.github/caller-stubs/"
      end
      # The technology library is a separate, opt-in set with the same
      # ref-substitution contract.
      output, status = Open3.capture2e(env, 'bash', File.join(ROOT, 'install.sh'), File.join(dir, 'tech'), 'main', 'tech')
      assert status.success?, output
      assert_equal TECH_COUNT, Dir[File.join(dir, 'tech/.github/workflows/*.yml')].size
      assert_empty Dir[File.join(dir, 'tech/.github/workflows/*.yml')].reject { |f| File.basename(f).start_with?('continuum-tech-') }
      Dir[File.join(dir, 'tech/.github/workflows/*.yml')].each do |file|
        job = yaml(file).fetch('jobs').fetch('call')
        assert_equal 'main', job.fetch('with').fetch('continuum_ref')
      end
      assert_includes File.read(env['DOWNLOAD_LOG']), '/main/.github/caller-stubs/tech/'
      output, status = Open3.capture2e(env, 'bash', File.join(ROOT, 'install.sh'), File.join(dir, 'techpinned'), 'release/v2', 'tech')
      assert status.success?, output
      Dir[File.join(dir, 'techpinned/.github/workflows/*.yml')].each do |file|
        job = yaml(file).fetch('jobs').fetch('call')
        assert job['uses'].end_with?('@release/v2')
        assert_equal 'release/v2', job.fetch('with').fetch('continuum_ref')
      end
      output, status = Open3.capture2e('bash', File.join(ROOT, 'install.sh'), File.join(dir, 'invalid'), 'bad&ref')
      refute status.success?, output
      refute Dir.exist?(File.join(dir, 'invalid'))
      # `swift` was the old technology-set name. The `continuum-tech-` prefix means "opt-in
      # library", and the value no longer exists.
      output, status = Open3.capture2e('bash', File.join(ROOT, 'install.sh'), File.join(dir, 'legacy'), 'main', 'swift')
      refute status.success?, output
      assert_includes output, "invalid set: swift (the old 'swift' set is now 'tech')"
      refute Dir.exist?(File.join(dir, 'legacy'))
    end
  end
  def test_parent_templates_match_reusable_contracts
    Dir[File.join(ROOT, '.github/caller-stubs/parent/*.yml')].each do |file|
      caller = yaml(file)
      caller.fetch('jobs').each_value do |job|
        next unless job['uses']
        name = job.fetch('uses').split('/').last.split('@').first
        callee = yaml(File.join(ROOT, '.github/workflows', name))
        assert_equal ['workflow_call'], events(callee).keys
        contract = events(callee).fetch('workflow_call')
        job.fetch('with').each_key { |key| assert contract.fetch('inputs').key?(key), key }
        contract.fetch('inputs').each do |key, spec|
          assert job.fetch('with').key?(key), key if spec['required']
        end
        assert job.fetch('secrets').key?('CHILD_RUNTIME_TOKEN')
        assert_equal 'main', job.fetch('with').fetch('engine_ref')
        callee.fetch('jobs').each_value do |inner|
          checkout = inner.fetch('steps').find { |step| step['name'] == 'Checkout Continuum engine' }
          assert_equal '${{ inputs.engine_ref }}', checkout.fetch('with').fetch('ref')
          assert inner.fetch('steps').any? { |step| step['run'].to_s.include?('CONTINUUM_ENGINE_ROOT=') }
        end
      end
    end
  end

  def test_parent_install_keeps_project_workflows_and_pins_engine
    fixture do |dir|
      destination = File.join(dir, 'consumer')
      workflows = File.join(destination, '.github/workflows')
      FileUtils.mkdir_p(workflows)
      preserved = %w[ci.yml release.yml issue-scheduler.yml opencode.yml]
      preserved.each { |name| File.write(File.join(workflows, name), "project-owned #{name}\n") }
      bin = File.join(dir, 'bin')
      FileUtils.mkdir_p(bin)
      File.write(File.join(bin, 'curl'), <<~SH)
        #!/usr/bin/env bash
        set -eu
        url="${@: -1}"
        [[ "$url" == */.github/caller-stubs/parent/* ]]
        cat "$TEMPLATES/${url##*/}"
      SH
      FileUtils.chmod(0755, File.join(bin, 'curl'))
      env = {'PATH'=>"#{bin}:#{ENV['PATH']}", 'TEMPLATES'=>File.join(ROOT, '.github/caller-stubs/parent')}
      ref = 'b' * 40
      output, status = Open3.capture2e(env, 'bash', File.join(ROOT, 'install.sh'), destination, ref, 'parent')
      assert status.success?, output
      preserved.each { |name| assert_equal "project-owned #{name}\n", File.read(File.join(workflows, name)) }
      # Strict contract: every installed caller carries the `continuum-` prefix,
      # so a project-owned `ci.yml` is never silently overwritten.
      assert_equal PARENT_COUNT, PARENT_STUBS.size, 'parent stub set drifted'
      installed = Dir[File.join(workflows, 'continuum-*.yml')]
      assert_equal PARENT_COUNT, installed.size
      assert_empty Dir[File.join(workflows, 'child-*.yml')]
      installed.each do |file|
        yaml(file).fetch('jobs').each_value do |job|
          next unless job['uses']
          assert job['uses'].end_with?("@#{ref}")
          assert_equal ref, job.fetch('with').fetch('engine_ref')
        end
      end
      output, status = Open3.capture2e('bash', File.join(ROOT, 'install.sh'), File.join(dir, 'invalid'), 'main', 'unknown')
      refute status.success?, output
      assert_includes output, 'invalid set: unknown'
      refute Dir.exist?(File.join(dir, 'invalid'))
      # `swift` was the technology-set name before the layer split; the layer
      # is now named `tech`, so the old value must be rejected outright.
      output, status = Open3.capture2e('bash', File.join(ROOT, 'install.sh'), File.join(dir, 'legacy'), 'main', 'swift')
      refute status.success?, output
      assert_includes output, 'invalid set: swift'
      refute Dir.exist?(File.join(dir, 'legacy'))
    end
  end

  # Continuum automation runs on free anonymous OpenCode models, so no core
  # workflow may read, require, or forward a paid provider token. There is no
  # exception: `pr-agent` cannot run without the paid Groq provider, so it must
  # report an explicit failure instead of a silent green no-op.
  def test_core_automation_requires_no_paid_provider_key
    core = Dir[File.join(ROOT, '.github/workflows/*.yml')].sort
    refute_empty core
    core.each do |path|
      name = File.basename(path)
      next if name.start_with?('continuum-tech-')

      body = File.read(path)
      %w[
        secrets.OPENCODE_API_KEY
        secrets.ANTHROPIC_API_KEY
        secrets.GROQ_API_KEY
        GROQ.KEY
      ].each do |needle|
        refute_includes body, needle, "#{name}: paid provider reference #{needle}"
      end
    end

    pr_agent = File.read(File.join(ROOT, '.github/workflows/pr-agent.yml'))
    assert_includes pr_agent, 'core.setFailed',
                    'pr-agent must fail explicitly, not skip green'
    assert_includes pr_agent, 'paid Groq provider',
                    'pr-agent must explain why it is disabled'
    refute_includes pr_agent, "if: steps.groq.outputs.available == 'true'"
  end

  # The free default model must be the single documented fallback everywhere an
  # OpenCode model is named, otherwise a consumer without OPENCODE_MODEL
  # silently runs a paid model.
  def test_opencode_model_default_is_the_free_model
    files = Dir[File.join(ROOT, '.github/workflows/*.yml')] +
            Dir[File.join(ROOT, '.github/caller-stubs/**/*.yml')]
    named = files.select { |path| File.read(path).include?('opencode/') }
    refute_empty named
    named.each do |path|
      next if File.basename(path).start_with?('continuum-tech-')

      body = File.read(path)
      refute_includes body, 'big-pickle', File.basename(path)
    end
    assert_includes File.read(File.join(ROOT, '.github/workflows/opencode.yml')),
                    "vars.OPENCODE_MODEL || 'opencode/muse-spark-1.3-contributor-free'"
  end

  # Automation limits are consumer policy: every hardcoded timeout/runner in a
  # core workflow must be overridable through a vars.AUTOMATION_* knob whose
  # default preserves the previously hardcoded value.
  def test_automation_limits_are_variable_driven
    core = Dir[File.join(ROOT, '.github/workflows/*.yml')].sort
    targets = %w[issue-scheduler.yml opencode.yml auto-merge.yml consumer-child-dispatcher.yml]
    targets.each do |name|
      path = File.join(ROOT, '.github/workflows', name)
      assert File.exist?(path), name
      body = File.read(path)
      body.scan(/^(\s*)timeout-minutes: (\d+)$/).each do |indent, minutes|
        flunk "#{name}: hardcoded timeout-minutes: #{minutes}"
      end
      assert_match(/vars\.AUTOMATION_\w+/, body, "#{name}: no AUTOMATION_* knob")
    end

    opencode = File.read(File.join(ROOT, '.github/workflows/opencode.yml'))
    assert_includes opencode, "vars.AUTOMATION_OPENCODE_RUNNER || 'macos-15'"
    assert_includes opencode, "vars.AUTOMATION_OPENCODE_TIMEOUT_MINUTES || '180'"

    scheduler = File.read(File.join(ROOT, '.github/workflows/issue-scheduler.yml'))
    assert_includes scheduler, "vars.AUTOMATION_WIP_LIMIT || '2'"
    assert_includes scheduler, "vars.AUTOMATION_LEASE_MINUTES || '45'"
    assert_includes scheduler, "vars.AUTOMATION_MAX_DISPATCH_ATTEMPTS || '2'"
  end

  # Core workflows must carry no product-specific path outside the opt-in
  # tech layer; the release version file is a consumer variable.
  def test_core_workflows_expose_no_product_paths
    core = Dir[File.join(ROOT, '.github/workflows/*.yml')].sort
    core.each do |path|
      name = File.basename(path)
      next if name.start_with?('continuum-tech-')

      body = File.read(path)
      # The version file may appear only as the fallback of a consumer
      # variable, never as a literal the engine acts on unconditionally.
      without_defaults = body
                        .gsub(/vars\.CONTINUUM_VERSION_FILE \|\| '[^']*'/, 'vars.CONTINUUM_VERSION_FILE')
                        .gsub(/vars\.CONTINUUM_RELEASE_MANIFEST_FILES \|\| '[^']*'/, 'vars.CONTINUUM_RELEASE_MANIFEST_FILES')
      refute_match(/Sources\/NanoDictateCore/, without_defaults, name)
      refute_match(/'nanodictate\.rb'/, without_defaults, name)
    end

    %w[auto-merge.yml add-review-label.yml].each do |name|
      body = File.read(File.join(ROOT, '.github/workflows', name))
      assert_includes body, "vars.CONTINUUM_VERSION_FILE || 'Sources/NanoDictateCore/Version.swift'", name
    end
  end

  # The dispatcher supplies the issue number; an issue_comment run must keep
  # reading the event, so the input defaults to empty and is only a fallback.
  def test_opencode_issue_number_input_is_optional_and_falls_back
    inputs = events(yaml(File.join(ROOT, '.github/workflows/opencode.yml')))
             .fetch('workflow_call').fetch('inputs')
    issue = inputs.fetch('issue_number')
    assert_equal '', issue.fetch('default')
    assert_equal false, issue.fetch('required')

    body = File.read(File.join(ROOT, '.github/workflows/opencode.yml'))
    assert_includes body, 'inputs.issue_number || github.event.issue.number'
  end

  # Every mode in the dispatch whitelist must have a step that acts on it, and
  # must actually be dispatched. A mode admitted to the whitelist with a
  # no-op body lets the job run Checkout/Install and finish SUCCESS having done
  # zero work — a silent green no-op. Matching the comparison string alone
  # cannot tell a real branch from `if: always() && inputs.mode == 'x'` +
  # `run: echo noop`, so the body of the branching step is checked too.
  INERT_BODY = /\A\s*(set\s+-[a-z]+\s*|:\s*|true\s*|exit\s+0\s*|echo\s+(noop|no-op|skip|done)?\s*)*\z/

  def test_opencode_whitelisted_modes_all_have_dispatch_steps
    body = workflow_body('opencode.yml')
    modes = dispatch_modes
    refute_empty modes
    assert_equal modes.uniq, modes, 'dispatching whitelist has duplicates'
    modes.each do |mode|
      assert_includes body, "inputs.mode == '#{mode}'",
                      "mode #{mode} is whitelisted but no step branches on it"
    end

    # Parse the dispatch job and require each whitelisted mode to own at least
    # one step that really does work, not just one that compares the mode.
    steps = yaml(File.join(ROOT, '.github/workflows/opencode.yml'))
              .fetch('jobs').fetch('opencode').fetch('steps')
    modes.each do |mode|
      branching = steps.select { |step| step['if'].to_s.include?("inputs.mode == '#{mode}'") }
      refute_empty branching, "mode #{mode} is whitelisted but has no branching step in the dispatch job"
      acts = branching.any? do |step|
        next true if step['uses']

        run = step['run'].to_s
        !run.strip.empty? && !run.match?(INERT_BODY)
      end
      assert acts, "mode #{mode} branches only on no-op steps — the job would finish SUCCESS having done nothing"
    end
  end

  # The whitelist is a contract with the callers: a mode nobody ever dispatches
  # is dead, and a dispatched mode the stub does not offer is rejected by
  # GitHub at run time, long after these tests went green.
  def test_every_whitelisted_mode_is_dispatched_by_a_core_workflow
    modes = dispatch_modes
    refute_empty modes
    calls = all_dispatch_calls
    refute_empty calls, 'no core workflow performs a /dispatches call'
    modes.each do |mode|
      dispatched = calls.any? do |call|
        call.include?(%(-f "inputs[mode]=#{mode}")) || call.include?(%(mode: '#{mode}'))
      end
      assert dispatched, "mode #{mode} is whitelisted but no core workflow dispatches it"
    end
  end

  # Each workflow that owns a dispatch call must still perform it. A call
  # swapped for `echo` leaves the file with no `/dispatches` window, so this
  # fails even though every other test still passes.
  def test_dispatching_workflows_still_perform_their_dispatch_call
    DISPATCHING_WORKFLOWS.each do |name|
      calls = dispatch_calls(name)
      refute_empty calls, "#{name} no longer performs a /dispatches call — the dispatch is a green no-op"
      calls.each do |call|
        assert_match(/--method POST|['"]POST \/repos|method:\s*'POST'/, call, "#{name}: dispatch call is not a POST")
      end
    end
  end

  # Every new knob is an additive input with a default that reproduces the
  # pre-existing behaviour, so existing stubs keep working untouched.
  def test_new_consumer_knobs_are_additive_with_safe_defaults
    scheduler = events(yaml(File.join(ROOT, '.github/workflows/issue-scheduler.yml')))
                  .fetch('workflow_call').fetch('inputs')
    {
      'wip_limit' => '',
      'lease_minutes' => '',
      'max_dispatch_attempts' => '',
      'dispatch_marker' => '<!-- issue-scheduler-dispatch -->',
      'in_progress_label' => 'automation:in-progress',
      'pause_marker' => 'automation:paused',
      'post_pause_comment' => 'true',
      'reset_markers' => 'false',
      'require_priority_label' => 'false',
      'command_grace_minutes' => '5',
      'child_owned_marker' => '<!-- continuum-child-owned -->',
      'legacy_child_owned_marker' => '<!-- runtime-worker-owned -->',
      'opencode_workflow_name' => 'OpenCode agent',
      'opencode_workflow_path' => '.github/workflows/continuum-opencode.yml'
    }.each do |key, default|
      assert scheduler.key?(key), "issue-scheduler missing input #{key}"
      assert_equal default, scheduler.fetch(key).fetch('default'), key
      assert_equal 'string', scheduler.fetch(key).fetch('type'), key
    end

    opencode = events(yaml(File.join(ROOT, '.github/workflows/opencode.yml')))
               .fetch('workflow_call').fetch('inputs')
    {
      'max_dispatch_attempts' => '',
      'dispatch_marker' => '',
      'in_progress_label' => '',
      'pause_marker' => '',
      'ci_workflow_id' => ''
    }.each do |key, default|
      assert opencode.key?(key), "opencode missing input #{key}"
      assert_equal default, opencode.fetch(key).fetch('default'), key
      assert_equal 'string', opencode.fetch(key).fetch('type'), key
    end

    # conflict_strategy: `workflow_call` cannot declare a `choice` input, so the
    # stub carries the enumerated list and the engine enforces it in the step.
    strategy = opencode.fetch('conflict_strategy')
    assert_equal 'string', strategy.fetch('type')
    assert_equal 'merge', strategy.fetch('default')
    stub = events(yaml(File.join(ROOT, '.github/caller-stubs/opencode.yml')))
           .fetch('workflow_dispatch').fetch('inputs').fetch('conflict_strategy')
    assert_equal 'choice', stub.fetch('type')
    assert_equal %w[merge checkout], stub.fetch('options')

    # The `mode` choice is the same kind of contract, and just as invisible: an
    # extra stub option is accepted by these tests but rejected by GitHub when
    # the call is actually made, and a missing one makes a whitelisted mode
    # unreachable for the operator.
    dispatch_inputs = events(yaml(File.join(ROOT, '.github/caller-stubs/opencode.yml')))
                     .fetch('workflow_dispatch').fetch('inputs').fetch('mode')
    assert_equal 'choice', dispatch_inputs.fetch('type')
    assert_equal dispatch_modes.sort, dispatch_inputs.fetch('options').sort,
                 'the stub mode choice and the engine dispatch whitelist must be the same set'

    body = File.read(File.join(ROOT, '.github/workflows/opencode.yml'))
    assert_match(/case "\$CONFLICT_STRATEGY" in/, body)
    assert_includes body, "expected 'merge' or 'checkout'"
    # The prompt must select a whole strategy block, not interpolate one verb.
    assert_includes body, 'STRATEGY_BLOCK'
  end

  # The parent/child workflows were parameterised in the same change as the
  # scheduler. They are the propagation edge, so a hardcoded label reappearing
  # in either file must fail here the same way it would fail in the scheduler.
  def test_parent_child_workflows_use_the_configured_labels
    dispatcher = workflow_body('consumer-child-dispatcher.yml')
    assert_includes dispatcher, "AUTOMATION_PAUSE_LABEL: ${{ vars.AUTOMATION_PAUSE_LABEL || 'automation:paused' }}"
    refute_match(/labels\/automation%3Apaused/, dispatcher,
                 'dispatcher must not hardcode the default pause label in a request URL')
    # The knob has to reach the actual label write, not only the env block.
    assert_match(%r{\$\{AUTOMATION_PAUSE_LABEL//:/%3A\}}, dispatcher,
                 'dispatcher must use the configured pause label in the label URL')
    assert_match(/--arg pause "\$AUTOMATION_PAUSE_LABEL"/, dispatcher,
                 'dispatcher must select stale pause labels by the configured label')

    review = workflow_body('consumer-child-review.yml')
    assert_includes review, "AUTOMATION_IN_PROGRESS_LABEL: ${{ vars.AUTOMATION_IN_PROGRESS_LABEL || 'automation:in-progress' }}"
    assert_includes review, "AUTOMATION_PAUSE_LABEL: ${{ vars.AUTOMATION_PAUSE_LABEL || 'automation:paused' }}"
    # Both labels are cleared in one loop; a hardcoded `for label in ...` list
    # would leave the consumer's own labels on every completed child issue.
    assert_match(/for label in "\$AUTOMATION_IN_PROGRESS_LABEL" "\$AUTOMATION_PAUSE_LABEL"; do/, review,
                 'child review must clear both configured labels, not a hardcoded list')
    assert_match(%r{\$\{label//:/%3A\}}, review,
                 'child review must use the configured label in the label URL')
  end

  # The documented semantics of post_pause_comment are subtle and the workflow
  # comment is the only place they are written down: an unset/empty value keeps
  # the comment, and only the exact string `false` disables it. Assert them on
  # the implementation rather than on the prose, so a mutated description or a
  # mutated comparison is both caught.
  def test_post_pause_comment_only_exact_false_disables_the_comment
    scheduler = events(yaml(File.join(ROOT, '.github/workflows/issue-scheduler.yml')))
                  .fetch('workflow_call').fetch('inputs').fetch('post_pause_comment')
    description = scheduler.fetch('description')
    assert_includes description, 'false',
                    'the description must name the exact value that disables the comment'
    assert_includes description, 'disables',
                    'the description must say which value turns the comment off'

    body = workflow_body('issue-scheduler.yml')
    expression = body[/postPauseComment\s*=\s*\n?\s*(\(process\.env\.POST_PAUSE_COMMENT \|\| '[^']*'\) [!==]+ '[^']*')/m, 1]
    refute_nil expression, 'postPauseComment is not derived from POST_PAUSE_COMMENT'
    # The env default must not itself be the disabling value, or an empty input
    # would silence the comment.
    fallback = expression[/process\.env\.POST_PAUSE_COMMENT \|\| '([^']*)'/, 1]
    refute_equal 'false', fallback,
                 'an empty POST_PAUSE_COMMENT must keep the comment enabled'
    refute_equal '', fallback, 'POST_PAUSE_COMMENT has no fallback default'

    # Re-implement the parsed expression in Ruby and run the documented cases
    # through it, so the assertion is on behaviour rather than on the wording.
    operator = expression[/\) (\S+) /, 1]
    expected = expression[/\) \S+ '([^']*)'/, 1]
    assert_includes %w[=== !==], operator, "unexpected comparison #{operator}"
    enabled = lambda do |value|
      subject = value.to_s.empty? ? fallback : value.to_s
      operator == '===' ? subject == expected : subject != expected
    end
    # The comment stays on for every value except the exact string `false`:
    # an empty input, an explicit true, and any other text all keep it enabled.
    ['', 'true', 'anything', 'False', 'FALSE', '0', 'no'].each do |value|
      assert enabled.call(value), "POST_PAUSE_COMMENT=#{value.inspect} must keep the pause comment enabled"
    end
    refute enabled.call('false'), "POST_PAUSE_COMMENT='false' must disable the pause comment"
  end

  # The scheduler's label/marker knobs must drive the code, not just be parsed.
  # Each assertion below fails if the corresponding implementation is deleted
  # while the input remains, which is exactly the dead-parameterisation bug.
  def test_scheduler_naming_knobs_drive_implementation
    body = File.read(File.join(ROOT, '.github/workflows/issue-scheduler.yml'))

    # post_pause_comment gates the actual comment creation.
    assert_match(/if \(postPauseComment\)/, body)
    assert_includes body, 'issues.createComment'

    # reset_markers gates the marker deletion.
    assert_match(/if \(resetMarkers\)/, body)
    assert_includes body, 'issues.deleteComment'

    # require_priority_label gates candidate selection.
    assert_match(/if \(requirePriorityLabel && priority === null\)/, body)

    # The scheduler env must be fed by the input with a matching fallback. The
    # watchdog's documented promise is that its markers match the scheduler's,
    # and both sides read the same `vars.AUTOMATION_*` value, so the fallback
    # chain has to include it here too.
    assert_includes body, "IN_PROGRESS_LABEL: ${{ inputs.in_progress_label || vars.AUTOMATION_IN_PROGRESS_LABEL || 'automation:in-progress' }}"
    assert_includes body, "PAUSE_LABEL: ${{ inputs.pause_marker || vars.AUTOMATION_PAUSE_LABEL || 'automation:paused' }}"
    assert_includes body, "DISPATCH_MARKER: ${{ inputs.dispatch_marker || vars.AUTOMATION_DISPATCH_MARKER || '<!-- issue-scheduler-dispatch -->' }}"
    assert_includes body, 'process.env.IN_PROGRESS_LABEL'
    assert_includes body, 'process.env.PAUSE_LABEL'
  end

  # opencode.yml must consume the same naming knobs as the scheduler on the
  # issue_comment path, where only vars are available, and must actually run the
  # consumer's blocking CI workflow when ci_workflow_id is set.
  def test_opencode_naming_knobs_and_ci_rerun_drive_implementation
    body = File.read(File.join(ROOT, '.github/workflows/opencode.yml'))

    assert_includes body, "DISPATCH_MARKER: ${{ inputs.dispatch_marker || vars.AUTOMATION_DISPATCH_MARKER || '<!-- issue-scheduler-dispatch -->' }}"
    assert_includes body, "IN_PROGRESS_LABEL: ${{ inputs.in_progress_label || vars.AUTOMATION_IN_PROGRESS_LABEL || 'automation:in-progress' }}"
    assert_includes body, "PAUSE_LABEL: ${{ inputs.pause_marker || vars.AUTOMATION_PAUSE_LABEL || 'automation:paused' }}"
    assert_includes body, 'process.env.DISPATCH_MARKER'
    assert_includes body, 'process.env.IN_PROGRESS_LABEL'
    assert_includes body, 'process.env.PAUSE_LABEL'

    # The env mapping above is only half the contract: the recovery job's `if:`
    # filters on the same value. Asserting only the env string would let a guard
    # that reads the old hardcoded default pass, and a consumer that set
    # AUTOMATION_DISPATCH_MARKER would get a job that never filters true.
    assert_includes body,
                    "contains(github.event.comment.body, inputs.dispatch_marker || vars.AUTOMATION_DISPATCH_MARKER || '<!-- issue-scheduler-dispatch -->')",
                    'recover-scheduled-issue must filter on the consumer-configured dispatch marker'

    assert_match(/gh workflow run "\$CI_WORKFLOW_ID"/, body)
  end

  # The macOS-only toolchain steps must stay guarded twice, so a non-macOS
  # AUTOMATION_OPENCODE_RUNNER never tries to install Swift or probe sw_vers.
  def test_opencode_macos_steps_are_guarded
    body = File.read(File.join(ROOT, '.github/workflows/opencode.yml'))
    assert_equal 2,
                 body.scan(/if: startsWith\(vars\.AUTOMATION_OPENCODE_RUNNER/).size
  end

  # A `vars.X || '0'`-style default that resolved to zero would make a job or a
  # backoff instant; no knob may be able to produce `timeout-minutes: 0`.
  def test_no_timeout_can_be_forced_to_zero
    Dir[File.join(ROOT, '.github/workflows/*.yml'), File.join(ROOT, '.github/caller-stubs/**/*.yml')].each do |path|
      next if File.basename(path).start_with?('continuum-tech-')

      refute_match(/timeout-minutes: 0/, File.read(path), File.basename(path))
    end
  end

  # ---------------------------------------------------------------- watchdog

  WATCHDOG = 'continuum-opencode-watchdog.yml'

  def watchdog_stub
    yaml(File.join(ROOT, '.github/caller-stubs', WATCHDOG))
  end

  def watchdog_body
    File.read(File.join(ROOT, '.github/workflows', WATCHDOG))
  end

  # The `workflow_run` filter matches a workflow's `name:` VALUE, never a file
  # name. Asserting the literal string "OpenCode agent" would pass even if the
  # OpenCode caller's `name:` were renamed, which is exactly the silent
  # breakage this guards: the filter is checked against the installed OpenCode
  # caller's own `name:`.
  def test_watchdog_trigger_tracks_the_watched_workflows_name
    opencode = yaml(File.join(ROOT, '.github/caller-stubs/opencode.yml'))
    watched = events(watchdog_stub).fetch('workflow_run').fetch('workflows')
    assert_equal [opencode.fetch('name')], watched,
                 'watchdog must watch the OpenCode caller by its `name:` value'
    assert_equal opencode.fetch('name'),
                 yaml(File.join(ROOT, '.github/workflows/opencode.yml')).fetch('name'),
                 'caller and callee `name:` must stay in lockstep for the workflow_run filter'

    # The engine must be told the same name, or its duplicate-run check would
    # never recognise a sibling OpenCode run and would fire a second retry.
    inputs = events(yaml(File.join(ROOT, '.github/workflows', WATCHDOG)))
             .fetch('workflow_call').fetch('inputs')
    assert_equal opencode.fetch('name'), watchdog_stub.fetch('jobs').fetch('call').fetch('with').fetch('watched_workflow')
    assert_equal '', inputs.fetch('watched_workflow').fetch('default')

    body = watchdog_body
    assert_match(/WATCHED_WORKFLOW: \$\{\{ inputs\.watched_workflow \}\}/, body)
    assert_match(/candidate\.name === watchedWorkflow/, body,
                 'the engine must use the configured workflow name to detect a retry already in flight')

    # The rename hazard is documented where a future editor will hit it.
    assert_match(/Renaming that `name:` silently disables this/, File.read(File.join(ROOT, '.github/caller-stubs', WATCHDOG)))
  end

  # The stub's permissions must cover the recovery job's writes.
  def test_watchdog_stub_grants_the_recovery_permissions
    rank = {'none'=>0, 'read'=>1, 'write'=>2}
    required = yaml(File.join(ROOT, '.github/workflows', WATCHDOG)).fetch('jobs').fetch('recover').fetch('permissions')
    granted = watchdog_stub.fetch('permissions')
    required.each do |key, value|
      assert_operator rank.fetch(granted.fetch(key, 'none')), :>=, rank.fetch(value), key
    end
  end

  # Every knob that used to be hardcoded in kodmai's full fork must be a
  # `workflow_call` input with a `vars.` fallback carrying the fork's own
  # default, so a consumer with empty repository variables still behaves.
  def test_watchdog_knobs_are_inputs_with_fork_default_fallbacks
    body = watchdog_body
    {
      'max_recovery_retries' => "MAX_RECOVERY_RETRIES: ${{ inputs.max_recovery_retries || vars.AUTOMATION_WATCHDOG_MAX_RETRIES || '1' }}",
      'retry_marker'         => "RETRY_MARKER: ${{ inputs.retry_marker || vars.AUTOMATION_WATCHDOG_RETRY_MARKER || '<!-- opencode-watchdog-retry -->' }}",
      'in_progress_label'    => "IN_PROGRESS_LABEL: ${{ inputs.in_progress_label || vars.AUTOMATION_IN_PROGRESS_LABEL || 'automation:in-progress' }}",
      'pause_marker'         => "PAUSE_LABEL: ${{ inputs.pause_marker || vars.AUTOMATION_PAUSE_LABEL || 'automation:paused' }}",
      'dispatch_marker'      => "DISPATCH_MARKER: ${{ inputs.dispatch_marker || vars.AUTOMATION_DISPATCH_MARKER || '<!-- issue-scheduler-dispatch -->' }}",
      'timeout_minutes'      => "fromJSON(inputs.timeout_minutes || vars.AUTOMATION_WATCHDOG_TIMEOUT_MINUTES || '5')"
    }.each do |input, env_line|
      assert_includes body, env_line, "#{input}: env mapping missing"
      definition = events(yaml(File.join(ROOT, '.github/workflows', WATCHDOG)))
                   .fetch('workflow_call').fetch('inputs').fetch(input)
      assert_equal '', definition.fetch('default'), "#{input} must default to empty so vars can supply it"
      assert_equal false, definition.fetch('required')
      assert_equal 'string', definition.fetch('type')
    end

    # The label/marker knobs must reach the code that acts on them, not only be
    # declared: a consumer setting AUTOMATION_PAUSE_LABEL to something else
    # would otherwise be paused under a label nothing ever reads.
    assert_match(/const pausedLabel = process\.env\.PAUSE_LABEL \|\| 'automation:paused'/, body)
    assert_match(/const inProgressLabel = process\.env\.IN_PROGRESS_LABEL \|\| 'automation:in-progress'/, body)
    assert_match(/const retryMarker = process\.env\.RETRY_MARKER/, body)
    assert_match(/const dispatchMarker = process\.env\.DISPATCH_MARKER/, body)
    assert_match(/Number\.parseInt\(\s*process\.env\.MAX_RECOVERY_RETRIES \|\| '1',/, body)

    # The scheduler's own knobs must resolve to the same values, or the watchdog
    # would unpause/reserve against a label the scheduler never checks.
    scheduler = File.read(File.join(ROOT, '.github/workflows/opencode.yml'))
    assert_includes scheduler, "IN_PROGRESS_LABEL: ${{ inputs.in_progress_label || vars.AUTOMATION_IN_PROGRESS_LABEL || 'automation:in-progress' }}"
    assert_includes scheduler, "PAUSE_LABEL: ${{ inputs.pause_marker || vars.AUTOMATION_PAUSE_LABEL || 'automation:paused' }}"
  end

  # The reason the watchdog exists: `opencode.yml`'s recovery only fires on a
  # scheduler dispatch comment, so a manual `/oc` run has no recovery path.
  # Asserting the retry body protects that path from silently becoming a no-op.
  def test_watchdog_recovery_body_dispatches_the_retry_comment
    body = watchdog_body
    # The reserve-then-retry sequence, ending at the real recovery comment.
    retry_comment = body[/await removeLabel\(pausedLabel\);.*\z/m]
    refute_nil retry_comment, 'the recovery dispatch step is gone'
    assert_match(/github\.rest\.issues\.createComment\(\{/, retry_comment)
    assert_includes retry_comment, "'/oc',"
    assert_includes retry_comment, 'retryMarker,'
    assert_match(/Automatic recovery retry/, retry_comment)
    # The retry must be reservation-aware, not a bare comment.
    assert_match(/await removeLabel\(pausedLabel\);/, retry_comment)
    assert_match(/await addLabel\(inProgressLabel\);/, retry_comment)

    # A PAT, not GITHUB_TOKEN: only a PAT fans the comment out into the
    # OpenCode caller's own `issue_comment` trigger.
    assert_includes body, 'github-token: ${{ secrets.TAP_PAT }}'

    # The engine must actually parse the run title it is handed, and must have a
    # fallback for consumers whose OpenCode caller sets no `run-name:` (Continuum's
    # own opencode.yml does not, so `display_title` is the issue title there).
    assert_match(%r{\(run\.display_title \|\| ''\)\.match\(\s*/\^OpenCode issue #\(\\d\+\)\$/\s*\)}, body,
                 'the engine must parse the numbered run title')
    assert_includes body, "run.event === 'issue_comment'"
    assert_includes body, 'item.title === run.display_title'
    assert_includes body, 'if (titleMatches.length === 1)'

    # Exhausting the budget must pause, not loop forever.
    assert_match(/if \(previousRetries >= maxRetries\) \{/, body)
    assert_includes body, 'await addLabel(pausedLabel);'
    assert_includes body, "'Remove the `' + pausedLabel + '` label and post `/oc` to retry manually.'"

    # Everything that makes a retry unsafe must be an early return.
    %w[
      issue.state !== 'open'
      issueLabels.has(pausedLabel)
      declaredOpenBlockers.length > 0
      openBlockers.length > 0
      if (issuePr) {
      if (anotherActiveRun) {
      if (newerSchedulerDispatch) {
    ].each do |guard|
      assert_includes body, guard
    end
    assert_includes body, "retryableConclusions.has(run.conclusion)"
    assert_equal %w[action_required failure stale startup_failure timed_out],
                 body[/const retryableConclusions = new Set\(\[(.*?)\]\);/m, 1].scan(/'(\w+)'/).flatten.sort
  end

  # The watchdog core file must never contain a runner-pinned condition: core
  # is capability-only.
  def test_watchdog_has_no_runner_pinned_guard
    refute_match(/if: startsWith\(runner/, watchdog_body)
  end

  # ------------------------------------------------- opencode-repair dispatch

  # A consumer's own CI reports its result by dispatching the installed
  # `continuum-opencode-repair.yml` caller with `gh workflow run`. Before this
  # path existed neither the core workflow nor its stub declared
  # `workflow_dispatch` at all, so that dispatch died with "Unexpected inputs"
  # and the only reason CI recovery appeared to work was a surviving fork in
  # the consumer repository.
  #
  # The four inputs are required on the stub and optional on the callee, and
  # both halves of that asymmetry are asserted: the stub must reject a partial
  # report, and the callee must still be callable with the four empty, because
  # its pull_request_target and workflow_run paths never supply them.
  REPORTED_CI_INPUTS = %w[pr_number head_sha conclusion run_id].freeze

  def test_opencode_repair_stub_accepts_a_reported_ci_result
    stub = yaml(File.join(ROOT, '.github/caller-stubs/opencode-repair.yml'))
    dispatch = events(stub).fetch('workflow_dispatch', nil)
    refute_nil dispatch,
                'the repair stub declares no workflow_dispatch: a consumer\'s CI report would be ' \
                'rejected with "Unexpected inputs", which is the bug this path fixes'
    assert_equal (REPORTED_CI_INPUTS + %w[ci_repair_label head_ref_pattern
                                          auto_merge_workflow opencode_workflow]).sort,
                 dispatch.fetch('inputs').keys.sort,
                 'the reported-CI-result dispatch contract plus its optional knobs'

    REPORTED_CI_INPUTS.each do |key|
      definition = dispatch.fetch('inputs').fetch(key)
      # Required on the stub: a result without the head it was measured on, or
      # without the PR it belongs to, is not actionable and would otherwise be
      # accepted and silently ignored.
      assert_equal true, definition.fetch('required'), "#{key}: a partial CI report must be rejected"
      assert_equal 'string', definition.fetch('type'), key
      refute_empty definition.fetch('description').to_s, "#{key}: the input must be documented"
    end

    # The knobs stay optional: the same stub is also triggered by
    # pull_request_target and workflow_run, where they have no value to carry.
    %w[ci_repair_label head_ref_pattern auto_merge_workflow opencode_workflow].each do |key|
      definition = dispatch.fetch('inputs').fetch(key)
      assert_equal false, definition.fetch('required'), "#{key}: a knob must not gate the dispatch"
      assert_equal 'string', definition.fetch('type'), key
    end

    callee_inputs = events(workflow_stub_callee).fetch('workflow_call').fetch('inputs')
    REPORTED_CI_INPUTS.each do |key|
      definition = callee_inputs.fetch(key)
      # Empty, not required: the two non-dispatch paths call this workflow with
      # no CI report at all, and a required callee input would reject them.
      assert_equal '', definition.fetch('default'), "#{key}: the callee input must default to empty"
      assert_equal false, definition.fetch('required'), "#{key}: the callee input must stay optional"
      assert_equal 'string', definition.fetch('type'), key
    end

    body = workflow_body('opencode-repair.yml')
    job = body[/^  ci-repair-dispatch:\n(.*?)(?=^  \S|\z)/m, 1]
    refute_nil job, 'opencode-repair.yml has no ci-repair-dispatch job'
    assert_includes job, "github.event_name == 'workflow_dispatch'",
                    'the dispatch job must be gated on the dispatch event alone'
    %w[pr_number head_sha conclusion].each do |key|
      assert_includes job, "inputs.#{key} != ''", "inputs.#{key} must gate the job against a partial report"
    end
  end

  # The dispatch path's guards, stale check, lock, and repair dispatch are the
  # whole mechanism this change ports. Assert the behaviour, not the shape: a
  # body that merely compares the conclusion and then falls through would pass
  # a presence check while burning nothing and repairing nothing.
  def test_opencode_repair_dispatch_body_guards_locks_and_repairs
    body = workflow_body('opencode-repair.yml')
    job = body[/^  ci-repair-dispatch:\n(.*?)(?=^  \S|\z)/m, 1]
    refute_nil job, 'opencode-repair.yml has no ci-repair-dispatch job'
    script = job[/run: \|\n(.*?)(?=^  \S|\z)/m, 1]
    refute_nil script, 'the dispatch job has no shell body'

    # Every guard must be an early exit, so a stale or foreign report costs
    # nothing. Guards, in the fork's order: state, repository, branch, staleness.
    [
      '"$pr_state" != "open"',
      '"$head_repo" != "$GITHUB_REPOSITORY"',
      '"$head_ref" =~ $HEAD_REF_PATTERN',
      '"$head_sha" != "$REPORTED_HEAD_SHA"'
    ].each { |guard| assert_includes script, guard, "missing guard: #{guard}" }
    assert_operator 4, :<=, script.scan('exit 0').size,
                    'each guard must be a skip, not a fall-through'

    # The lock is one attempt per head: check before add, and the ci-fix
    # dispatch happens only after the label is on the PR.
    lock_check = script.index('grep -Fxq "$CI_REPAIR_LABEL"')
    lock_add = script.index(%(-f "labels[]=$CI_REPAIR_LABEL"))
    ci_fix = script.index('inputs[mode]=ci-fix')
    refute_nil lock_check, 'the lock check is gone: repair would repeat on every report'
    refute_nil lock_add, 'the lock is never taken'
    refute_nil ci_fix, 'no ci-fix dispatch remains on the dispatch path'
    assert_operator lock_check, :<, ci_fix, 'the lock must be checked before repair is dispatched'
    assert_operator lock_add, :<, ci_fix, 'the lock must be taken before repair is dispatched'

    # Success clears the lock and wakes the reconciler; it must never dispatch
    # a repair, which is the one ordering that would auto-fix a green PR.
    success = script.index('"$REPORTED_CONCLUSION" == "success"')
    assert_operator success, :<, ci_fix, 'success must be handled before the repair dispatch'
    between = script[success...ci_fix]
    assert_includes between, 'labels/$CI_REPAIR_LABEL', 'success must clear the repair lock'
    assert_includes between, 'actions/workflows/$AUTO_MERGE_WORKFLOW/dispatches',
                    'success must wake the auto-merge reconciler'

    # A non-repairable conclusion must not consume the one attempt the head has.
    assert_includes script, '"$REPORTED_CONCLUSION" != "failure" && "$REPORTED_CONCLUSION" != "timed_out"'

    # A failed dispatch must release the lock, or the head can never retry.
    tail = script[ci_fix..]
    assert_includes tail, 'labels/$CI_REPAIR_LABEL', 'a failed repair dispatch must clear the lock'

    # The reported run id is what ci-fix reruns; dropping it turns the dispatch
    # into a repair with no run to point at.
    assert_includes script, '-f "inputs[run_id]=$REPORTED_RUN_ID"'

    # The PAT, not GITHUB_TOKEN: the whole point of this path is that the
    # reporting CI may live outside this repository's default token scope.
    assert_includes job, 'GH_TOKEN: ${{ secrets.TAP_PAT }}'

    # A dispatch target reached by path must be an env var, never a literal file
    # name, so it can carry the `continuum-` prefix that survives installation.
    %w[$OPENCODE_WORKFLOW $AUTO_MERGE_WORKFLOW].each do |target|
      bare = target.delete('$')
      assert_includes script, "actions/workflows/#{target}/dispatches", "#{target} is never dispatched"
      assert_match(/^\s+#{bare}: \$\{\{ inputs\./, job, "#{target} must be an env var bound to an input")
    end
  end

  # Every knob the dispatch path introduced must fall back to the value this
  # controller used before it was parameterized, so a consumer with empty
  # repository variables is unaffected.
  def test_opencode_repair_knobs_are_additive_with_safe_defaults
    inputs = events(workflow_stub_callee).fetch('workflow_call').fetch('inputs')
    {
      'ci_repair_label' => '',
      'head_ref_pattern' => '',
      'auto_merge_workflow' => '',
      'opencode_workflow' => ''
    }.each do |key, default|
      assert inputs.key?(key), "opencode-repair missing input #{key}"
      assert_equal default, inputs.fetch(key).fetch('default'), key
      assert_equal 'string', inputs.fetch(key).fetch('type'), key
      assert_equal false, inputs.fetch(key).fetch('required'), key
    end

    body = workflow_body('opencode-repair.yml')
    {
      'CI_REPAIR_LABEL' => ['ci_repair_label', 'CONTINUUM_CI_REPAIR_LABEL', 'opencode-ci-repair'],
      'HEAD_REF_PATTERN' => ['head_ref_pattern', 'CONTINUUM_HEAD_REF_PATTERN', '^opencode/issue[0-9]+-'],
      'AUTO_MERGE_WORKFLOW' => ['auto_merge_workflow', 'CONTINUUM_AUTO_MERGE_WORKFLOW', 'continuum-auto-merge.yml'],
      'OPENCODE_WORKFLOW' => ['opencode_workflow', 'CONTINUUM_OPENCODE_WORKFLOW', 'continuum-opencode.yml']
    }.each do |key, (input, variable, literal)|
      assert_includes body, "#{key}: \${{ inputs.#{input} || vars.#{variable} || '#{literal}' }}",
                      "#{key}: missing inputs/vars/literal fallback"
    end

    # Re-evaluate the chain rather than re-asserting it: a reordered or
    # mistyped default that still contains the right literal would otherwise
    # pass this test and change every consumer's behaviour.
    body.lines.select { |line| line.include?('CI_REPAIR_LABEL: ${{') }.each do |line|
      terms = line[/\$\{\{(.*)\}\}/, 1].split('||').map(&:strip)
      assert_equal 'inputs.ci_repair_label', terms.first, line
      assert_includes terms[1], 'vars.CONTINUUM', line
      resolve = lambda do |inputs_value, vars_value|
        terms.reduce(nil) do |acc, term|
          acc || case term
                 when /\A'([^']*)'\z/ then Regexp.last_match(1)
                 when /\Avars\./ then vars_value
                 when /\Ainputs\./ then inputs_value
                 end
        end
      end
      assert_equal 'opencode-ci-repair', resolve.call(nil, nil),
                   'empty inputs and vars must still yield the pre-existing label'
      refute_equal 'opencode-ci-repair', resolve.call(nil, 'consumer-value'),
                   'a set vars. value must win over the default'
    end

    # The stub must pass every knob through bare, or a pinned literal would
    # silently override the consumer's variable for all installed callers.
    with = yaml(File.join(ROOT, '.github/caller-stubs/opencode-repair.yml'))
           .fetch('jobs').fetch('call').fetch('with')
    %w[pr_number head_sha conclusion run_id ci_repair_label head_ref_pattern
       auto_merge_workflow opencode_workflow].each do |key|
      assert_equal "\${{ inputs.#{key} }}", with.fetch(key), "#{key} must be a bare passthrough"
    end

    # The lock the dispatch path writes is the same one the pre-existing
    # synchronize reset already clears, or a new head could not repair.
    assert_includes body, 'for label in "$CI_REPAIR_LABEL" opencode-packaging-smoke-repair; do',
                    'the per-head reset must clear the configured lock, not a hardcoded one'
    # The workflow_run path keeps its looser `opencode/*` guard: tightening it
    # would stop repairing heads this controller repaired before.
    assert_includes body, '"$head_ref" != opencode/*'
  end

  # ------------------------------------------------- render execution controller

  RENDER = 'continuum-render-executor.yml'

  def render_stub
    yaml(File.join(ROOT, '.github/caller-stubs', RENDER))
  end

  def render_core
    yaml(File.join(ROOT, '.github/workflows', RENDER))
  end

  def render_body
    File.read(File.join(ROOT, '.github/workflows', RENDER))
  end

  # The fork this core file was ported from hardcoded its Render API token to
  # a `KEY` secret. No Continuum consumer is required to define that name, so
  # the same wiring would resolve to an empty token in every installed caller.
  # The secret contract is TAP_PAT, and this workflow may not quietly keep a
  # second spelling.
  def test_render_executor_reads_tap_pat_and_never_the_fork_key_secret
    body = render_body
    refute_includes body, 'secrets.KEY',
                    'the fork read secrets.KEY; Continuum secrets are named TAP_PAT by contract'
    # Both Render steps must be wired, not just one: the execute step creating
    # the worker and the cleanup step deleting it need the same token, and a
    # cleanup that lost it would leave an ephemeral worker running.
    assert_equal 2, body.scan(/RENDER_API_KEY: \$\{\{ secrets\.TAP_PAT \}\}/).size,
                 'both the execute and the mandatory cleanup step must read TAP_PAT'
    # The classification step already used the PAT-with-token-fallback chain.
    assert_includes body, 'GH_TOKEN: ${{ secrets.TAP_PAT || github.token }}'
  end

  # Every value the fork hardcoded for one repository must be a knob, or a
  # second consumer inherits that repository's paths and markers.
  def test_render_executor_knobs_cover_every_fork_hardcoded_value
    body = render_body
    {
      'RENDER_REGION' => ['render_region', 'RENDER_REGION', 'oregon'],
      'RENDER_STATE_FILE' => ['state_file', 'RENDER_STATE_FILE', '/tmp/continuum-render-state.json'],
      'RENDER_RESULT_FILE' => ['result_file', 'RENDER_RESULT_FILE', '/tmp/continuum-render-result.json'],
      'RENDER_MEMORY_SUMMARY_FILE' => ['memory_summary_file', 'RENDER_MEMORY_SUMMARY_FILE', '/tmp/continuum-render-memory-summary.json'],
      'RENDER_QUAL_RESULT_FILE' => ['qualification_result_file', 'RENDER_QUALIFICATION_RESULT_FILE', '/tmp/continuum-render-qualification.json'],
      'MAX_RENDER_REPAIR_ATTEMPTS' => ['max_repair_attempts', 'MAX_RENDER_REPAIR_ATTEMPTS', '10'],
      'QUALIFICATION_LABEL' => ['qualification_label', 'RENDER_QUALIFICATION_LABEL', 'qualification:render'],
      'QUALIFICATION_MARKER' => ['qualification_marker', 'RENDER_QUALIFICATION_MARKER', '<!-- continuum-render-qualification-result -->'],
      'ARTIFACT_PREFIX' => ['artifact_prefix', 'RENDER_ARTIFACT_PREFIX', 'render-qualification'],
      'DISPATCH_REF' => ['dispatch_ref', 'RENDER_DISPATCH_REF', 'main'],
      'JOB_SCRIPT' => ['job_script', 'RENDER_JOB_SCRIPT', 'automation/render-job.sh'],
      'CLEANUP_SCRIPT' => ['cleanup_script', 'RENDER_CLEANUP_SCRIPT', 'automation/render-cleanup.sh'],
      'QUALIFICATION_SCRIPT' => ['qualification_script', 'RENDER_QUALIFICATION_SCRIPT', 'automation/record_render_qualification.py'],
      'IN_PROGRESS_LABEL' => ['in_progress_label', 'AUTOMATION_IN_PROGRESS_LABEL', 'automation:in-progress'],
      'PAUSE_LABEL' => ['pause_label', 'AUTOMATION_PAUSE_LABEL', 'automation:paused'],
      'REPAIR_LABEL' => ['repair_label', 'AUTOMATION_REPAIR_LABEL', 'priority:p0'],
      'E2E_BRANCH_PREFIX' => ['e2e_branch_prefix', 'RENDER_E2E_BRANCH_PREFIX', 'opencode/issue'],
      'SCHEDULER_WORKFLOW' => ['scheduler_workflow', 'RENDER_SCHEDULER_WORKFLOW', 'continuum-issue-scheduler.yml'],
      'OPENCODE_MODEL' => ['model', 'OPENCODE_MODEL', nil]
    }.each do |env_key, (input, variable, literal)|
      definition = events(render_core).fetch('workflow_call').fetch('inputs').fetch(input)
      assert_equal '', definition.fetch('default'), "#{input} must default to empty so vars can supply it"
      assert_equal false, definition.fetch('required'), input
      assert_equal 'string', definition.fetch('type'), input
      # The chain is `inputs.x || vars.VAR`, and only the model has no literal.
      expected = literal ? "inputs.#{input} || vars.#{variable} || '#{literal}'" : "inputs.#{input} || vars.#{variable}"
      assert_includes body, "#{env_key}: \${{ #{expected} }}", "#{env_key}: env mapping missing"
    end

    # The model is the one knob with no default anywhere in the chain, so the
    # run must fail explicitly rather than start a worker that executes nothing.
    refute_includes body, "vars.OPENCODE_MODEL || '",
                    'OPENCODE_MODEL must have no literal default'
    assert_includes body, '[[ -n "$OPENCODE_MODEL" ]] || {'
    assert_includes body, 'echo "::error::No OpenCode model configured'
  end

  # The fork's region, model, paths, labels and markers all had to become
  # inputs. This is asserted over the workflows ported out of that fork, so
  # the next port cannot reintroduce a repository-specific literal.
  def test_the_ported_qualification_workflows_carry_no_fork_literal
    {
      RENDER => ['kodmial/runtime-lab', 'runtime-lab-render', '--project runtime-lab',
                 '<!-- runtime-lab-render-qualification-result -->']
    }.each do |base, needles|
      body = File.read(File.join(ROOT, '.github/workflows', base))
      needles.each do |needle|
        refute_includes body, needle, "#{base}: fork-specific literal #{needle}"
      end
    end
  end

  # `mode` is the fork's execution contract: smoke closes the issue, e2e looks
  # for a result PR. Both halves must stay, or the run reports success having
  # resolved nothing.
  def test_render_executor_gates_the_mode_and_keeps_both_branches
    body = render_body
    assert_includes body, '[[ "$EXECUTION_MODE" == "smoke" || "$EXECUTION_MODE" == "e2e" ]] || {'
    assert_includes body, 'echo "::error::Unsupported execution mode: $EXECUTION_MODE"'
    assert_includes body, '[[ -n "$ISSUE_NUMBER" ]] || {'
    assert_includes body, 'if [[ "$EXECUTION_MODE" == "smoke" ]]; then'
    assert_includes body, 'gh issue close "$ISSUE_NUMBER"'
    assert_includes body, '--reason completed'
    # e2e must still require a result PR, selected by the configured prefix.
    assert_includes body, '[[ -z "$PR_NUMBER" ]]'
    assert_includes body, '--arg prefix "${E2E_BRANCH_PREFIX}${ISSUE_NUMBER}-"'
    assert_includes body, 'echo "::error::E2E mode completed without the required PR"'

    # The artifact name is built from the configured prefix, not a literal.
    assert_includes body, 'name: ${{ env.ARTIFACT_PREFIX }}-${{ inputs.issue_number }}-${{ github.run_id }}'
    # The qualification label must be read from the configured value, not
    # matched literally, or a renamed label classifies nothing.
    assert_includes body, "jq -r --arg label \"$QUALIFICATION_LABEL\" '[.labels[].name] | index($label) != null'"
    assert_includes body, 'jq -e --arg label "$QUALIFICATION_LABEL"'
    # The result marker is a variable, not the fork's literal.
    assert_includes body, 'printf \'%s\n%s\' "$QUALIFICATION_MARKER" "$BODY"'
    refute_includes body, '<!-- runtime-lab-render-qualification-result -->'
  end

  # The stub must reject a partial dispatch. `mode` decides whether the issue
  # is closed or handed to a PR chain, so accepting a run without it would
  # dispatch an execution nobody described.
  def test_render_executor_stub_requires_its_dispatch_inputs
    dispatch = events(render_stub).fetch('workflow_dispatch')
    %w[issue_number mode].each do |key|
      definition = dispatch.fetch('inputs').fetch(key)
      assert_equal true, definition.fetch('required'), "#{key} must be required"
      assert_equal 'string', definition.fetch('type'), key
      refute_empty definition.fetch('description').to_s, key
    end
    # Every remaining input stays optional, because a scheduler dispatch names
    # only the issue and the mode.
    (dispatch.fetch('inputs').keys - %w[issue_number mode]).each do |key|
      assert_equal false, dispatch.fetch('inputs').fetch(key).fetch('required'),
                   "#{key} must not gate the dispatch"
    end

    # The callee keeps both optional and empty: the engine re-checks them, so a
    # reusable call with neither still fails loudly instead of acting on an
    # empty issue number.
    callee = events(render_core).fetch('workflow_call').fetch('inputs')
    %w[issue_number mode].each do |key|
      assert_equal '', callee.fetch(key).fetch('default'), key
      assert_equal false, callee.fetch(key).fetch('required'), key
    end
  end

  # ------------------------------------------------- stub input contract

  # Every `with:` key each core stub is allowed to pass. A stub is installed
  # verbatim into a consumer repository, so a key added here is a decision
  # Continuum makes on every consumer's behalf and needs a test edit.
  STUB_INPUT_WHITELIST = {
    'add-review-label.yml' => %w[continuum_ref],
    'auto-merge.yml' => %w[continuum_ref],
    'bootstrap-runtime-secret.yml' => %w[continuum_ref],
    'coderabbit-retry.yml' => %w[continuum_ref],
    'coderabbit-unresolved.yml' => %w[continuum_ref],
    'continuum-opencode-watchdog.yml' => %w[continuum_ref watched_workflow],
    'continuum-render-executor.yml' => %w[
      continuum_ref issue_number mode render_region model state_file result_file
      memory_summary_file qualification_result_file max_repair_attempts
      qualification_label qualification_marker artifact_prefix dispatch_ref
      job_script cleanup_script qualification_script in_progress_label
      pause_label repair_label e2e_branch_prefix chain_workflow
      scheduler_workflow concurrency_group timeout_minutes
    ],
    'issue-scheduler.yml' => %w[
      continuum_ref wip_limit lease_minutes max_dispatch_attempts dispatch_marker
      in_progress_label pause_marker post_pause_comment reset_markers
      require_priority_label command_grace_minutes child_owned_marker
      legacy_child_owned_marker opencode_workflow_name opencode_workflow_path
    ],
    'opencode.yml' => %w[
      continuum_ref mode issue_number pr_number head_ref review_id run_id
      ci_workflow_id conflict_strategy dispatch_marker in_progress_label
      pause_marker max_dispatch_attempts
    ],
    'opencode-repair.yml' => %w[
      continuum_ref pr_number head_sha conclusion run_id ci_repair_label
      head_ref_pattern auto_merge_workflow opencode_workflow
    ],
    'opencode-unresolved.yml' => %w[continuum_ref],
    'pr-agent.yml' => %w[continuum_ref],
    'remove-review-label.yml' => %w[continuum_ref]
  }.freeze

  # A stub that pins an input to a literal overrides the consumer's own
  # `vars.AUTOMATION_*` / `vars.CONTINUUM_*` value for every installed caller:
  # `inputs.x || vars.X || 'default'` can never see the variable once `inputs.x`
  # is a non-empty literal. Passing `require_coderabbit: 'true'` in the
  # auto-merge stub therefore silently switches CodeRabbit on for every
  # consumer, and passing `max_recovery_retries: '9'` in the watchdog stub
  # silently overrides `AUTOMATION_WATCHDOG_MAX_RETRIES`. Neither is visible in
  # the engine, so nothing else fails.
  #
  # Every value must therefore be a bare `inputs.*` passthrough — an empty
  # string is what lets the callee fall through to its `vars.` default. The one
  # exception is `watched_workflow`, which is a `name:` binding rather than a
  # vars-backed knob: it must be pinned, and it is pinned to the OpenCode
  # caller's own `name:` by the watchdog trigger test above.
  def test_stubs_never_pin_a_consumer_knob
    whitelist = STUB_INPUT_WHITELIST
    assert_equal CORE_STUBS.map { |path| File.basename(path) }.sort,
                 whitelist.keys.sort,
                 'every core stub needs a `with:` whitelist entry'

    whitelist.each do |base, allowed|
      path = File.join(ROOT, '.github/caller-stubs', base)
      with = yaml(path).fetch('jobs').fetch('call').fetch('with')

      # The whitelist is the contract: a new key is a new pinned decision.
      with.each_key do |key|
        assert_includes allowed, key,
                        "#{base}: passes `#{key}`, which the stub whitelist does not allow"
      end

      with.each do |key, value|
        next if key == 'continuum_ref'
        next if key == 'watched_workflow'

        assert_match(/\A\$\{\{ inputs\.#{key}/, value.to_s,
                     "#{base}: `#{key}` must be a bare inputs.* passthrough, got #{value.inspect} — " \
                     'a literal silently overrides the consumer\'s repository variable')
      end
    end
  end

  # The passthrough whitelist is only half the contract: a stub could pin a
  # knob to a literal *and* the engine could then ignore the variable. Assert
  # that each stub-covered knob really does reach a `vars.` fallback, so the
  # consumer's repository variable is the live source of truth.
  def test_every_stub_parameterized_knob_is_backed_by_a_vars_fallback
    scheduler = File.read(File.join(ROOT, '.github/workflows/issue-scheduler.yml'))
    {
      'WIP_LIMIT' => %w[wip_limit AUTOMATION_WIP_LIMIT 2],
      'LEASE_MINUTES' => %w[lease_minutes AUTOMATION_LEASE_MINUTES 45],
      'MAX_DISPATCH_ATTEMPTS' => %w[max_dispatch_attempts AUTOMATION_MAX_DISPATCH_ATTEMPTS 2],
      'DISPATCH_MARKER' => ['dispatch_marker', 'AUTOMATION_DISPATCH_MARKER', '<!-- issue-scheduler-dispatch -->'],
      'IN_PROGRESS_LABEL' => ['in_progress_label', 'AUTOMATION_IN_PROGRESS_LABEL', 'automation:in-progress'],
      'PAUSE_LABEL' => ['pause_marker', 'AUTOMATION_PAUSE_LABEL', 'automation:paused']
    }.each do |env_key, (input, variable, literal)|
      assert_includes scheduler,
                      "#{env_key}: \${{ inputs.#{input} || vars.#{variable} || '#{literal}' }}",
                      "issue-scheduler: #{env_key} has no vars. fallback"
    end

    assert_includes auto_merge_body,
                    "REQUIRE_CODERABBIT: ${{ inputs.require_coderabbit || vars.CONTINUUM_REQUIRE_CODERABBIT || 'false' }}"
    assert_includes watchdog_body,
                    "MAX_RECOVERY_RETRIES: ${{ inputs.max_recovery_retries || vars.AUTOMATION_WATCHDOG_MAX_RETRIES || '1' }}"
  end

  # Adding a `vars.` fallback to a `inputs.x || 'literal'` chain must not change
  # the value a consumer with empty repository variables gets: the empty
  # variable is falsy, so the chain still falls through to the same literal.
  # Re-implement the chain in Ruby and evaluate it, so a reordered or
  # mistyped default is caught rather than merely re-asserted.
  # ------------------------------------------ scheduler guards from the fork
  #
  # These guards lived only in the consumer's full fork of the scheduler. They
  # are now core behaviour, so each one is asserted on the code that acts on it:
  # deleting the guard while leaving the knob (or the marker) in place is the
  # silent regression this test exists to catch.

  # The declared-dependency marker is parsed by the scheduler AND by the
  # watchdog. A doubled backslash in a YAML-embedded regex literal is a legal,
  # silently non-matching regex — the guard would look present and block
  # nothing. Assert the single-backslash form in both files.
  def test_scheduler_declared_blocker_regex_is_not_double_escaped
    { 'issue-scheduler.yml' => workflow_body('issue-scheduler.yml'),
      WATCHDOG => watchdog_body }.each do |name, body|
      assert_includes body, '/<!--\s*automation-blocked-by:\s*([0-9#\s,]+?)\s*-->/i',
                      "#{name}: the automation-blocked-by regex must use single backslashes"
      # The doubled form is spelled as raw bytes, not as one of Ruby's own
      # escape spellings: `'\\s'` in a single-quoted string is the CORRECT
      # single-backslash regex, so asserting against it would fire on good code.
      doubled = 'automation-blocked-by:' + ('\\' * 2) + 's'
      refute_includes body, doubled,
                      "#{name}: doubled backslash in the automation-blocked-by regex matches nothing"
    end

    scheduler = workflow_body('issue-scheduler.yml')
    # It must be a live parser, not a literal: numbers are extracted and read.
    assert_includes scheduler, '[...match[1].matchAll(/\d+/g)]'
    assert_includes scheduler, 'async function openDeclaredBlockers(issue) {'
    # …and it must actually gate candidate selection.
    assert_includes scheduler, 'const declaredOpenBlockers = await openDeclaredBlockers(issue);'
    assert_includes scheduler, "': declared blocked by '"
  end

  # An issue carrying a child-owned marker belongs to a delegated worker. Both
  # the candidate filter and the just-in-time re-check must skip it, and the
  # marker must come from a knob rather than a literal.
  def test_scheduler_skips_child_owned_issues_everywhere
    scheduler = workflow_body('issue-scheduler.yml')
    assert_match(/function isChildOwned\(issue\) \{\s*\n\s*const body = issue\.body \|\| '';\s*\n\s*return \(\s*\n\s*body\.includes\(childOwnedMarker\) \|\|\s*\n\s*body\.includes\(legacyChildOwnedMarker\)/, scheduler,
                 'isChildOwned must honour both the current and the legacy marker')

    assert_includes scheduler, "CHILD_OWNED_MARKER: ${{ inputs.child_owned_marker || vars.CONTINUUM_CHILD_OWNED_MARKER || '<!-- continuum-child-owned -->' }}"
    assert_includes scheduler, "LEGACY_CHILD_OWNED_MARKER: ${{ inputs.legacy_child_owned_marker || vars.CONTINUUM_LEGACY_CHILD_OWNED_MARKER || '<!-- runtime-worker-owned -->' }}"

    # Call sites only — the `function isChildOwned(issue)` definition line is not
    # a filter, it is the helper itself.
    calls = scheduler.scan(/(?<!function )isChildOwned\((issue|freshIssue)\)/).flatten
    assert_equal %w[freshIssue issue], calls.sort,
                 'the child-owned filter must run both at selection time and just before dispatch'

    # The marker literals must live only in the fallback chain, not inline in
    # the skip logic, or a consumer renaming its marker would be ignored. The
    # assertion runs over the embedded script only — the `workflow_call`
    # defaults are supposed to name these values.
    engine = script_body('issue-scheduler.yml')
    engine.lines.grep(/<!-- (continuum|runtime-worker)-(child-owned|owned) -->/).each do |line|
      # A marker may appear in the engine only as the tail of a fallback chain
      # (env, then `||` literal). Anywhere else — in isChildOwned, in the
      # candidate filter, in the dispatch re-check — it would silently ignore a
      # consumer that renamed its marker.
      assert_match(/\|\|\s*'<!--\s/, line,
                   "marker literal used outside a fallback chain: #{line.strip}")
    end
    assert_includes engine, 'function isChildOwned(issue) {'
  end

  # A manual owner `/oc` is real in-flight work: reserve it at once, and keep a
  # short grace window so this run cannot enqueue a duplicate right behind it.
  def test_scheduler_reserves_owner_commands_and_honours_the_grace_window
    scheduler = workflow_body('issue-scheduler.yml')

    assert_includes scheduler, "COMMAND_GRACE_MINUTES: ${{ inputs.command_grace_minutes || vars.AUTOMATION_COMMAND_GRACE_MINUTES || '5' }}"
    assert_includes scheduler, "const commandGraceMinutes = positiveInt('COMMAND_GRACE_MINUTES', 5);"
    assert_includes scheduler, 'const commandGraceMs = commandGraceMinutes * 60 * 1000;'
    # The grace window must not inherit the full lease, and it must be the
    # grace value the guard actually compares against.
    assert_includes scheduler, 'if (commandAgeMs < commandGraceMs) {'
    refute_includes scheduler, 'if (commandAgeMs < leaseMs) {',
                    'the owner-command guard must use the grace window, not the work lease'

    reservation = scheduler[/if \(context\.eventName === 'issue_comment'\) \{\s*\n\s*const eventIssue.*?\n            \}/m]
    refute_nil reservation, 'the owner-command reservation block is gone'
    assert_includes reservation, 'comment?.user?.login === owner'
    assert_includes reservation, "body.includes('/oc') || body.includes('/opencode')"
    assert_includes reservation, 'await addLabel(eventIssue.number, inProgressLabel);'
    assert_includes reservation, 'Reserved issue #'

    # A manual command must not consume the scheduler's own attempt budget, or
    # two manual runs would pause an issue the scheduler never dispatched.
    assert_includes scheduler, 'const schedulerDispatches = comments.filter(comment =>'
    assert_includes scheduler, 'if (schedulerDispatches.length >= maxDispatchAttempts) {'
    refute_includes scheduler, 'if (dispatches.length >= maxDispatchAttempts) {'
  end

  # The just-in-time re-check is the guard against a duplicate OpenCode run: it
  # runs after selection and before the reservation/comment, and every unsafe
  # condition has to be a skip.
  def test_scheduler_rechecks_state_just_before_dispatch
    scheduler = workflow_body('issue-scheduler.yml')
    dispatch = scheduler[/for \(const \{ issue, priority \} of selected\) \{\n(.*?)\n              await addLabel\(issue\.number, inProgressLabel\);/m, 1]
    refute_nil dispatch, 'the just-in-time re-check block is gone'

    [
      'const freshIssueResponse = await github.rest.issues.get({',
      "freshIssue.state !== 'open'",
      'freshLabels.has(pausedLabel)',
      'freshLabels.has(inProgressLabel)',
      'const freshDeclaredBlockers = await openDeclaredBlockers(freshIssue);',
      'freshOpenBlockers.length > 0',
      'if (commandAgeMs < commandGraceMs) {'
    ].each { |guard| assert_includes dispatch, guard, "missing just-in-time guard: #{guard}" }
    assert_equal 5, dispatch.scan(/^\s+continue;\s*$/).size,
                 'every just-in-time guard must be a skip, not a fall-through'
  end

  # A blocked issue whose OpenCode PR was closed unmerged is not a failed
  # implementation, and a native blocker is authoritative over an old
  # reservation lease. Both must release, not pause.
  def test_scheduler_releases_native_blockers_instead_of_pausing
    scheduler = workflow_body('issue-scheduler.yml')

    # pull_request_target reconciliation.
    pr_close = scheduler[/if \(context\.eventName === 'pull_request_target'\).*?\n            \}\n/m]
    refute_nil pr_close, 'the pull_request_target reconciliation block is gone'
    assert_includes pr_close, 'const openBlockers = blockers.filter('
    assert_includes pr_close, 'if (openBlockers.length > 0) {'
    assert_includes pr_close, 'reservation released, issue remains '
    assert_includes pr_close, 'await pauseIssue('
    # The release must come before the pause, or blocked work still pauses.
    assert_operator pr_close.index('reservation released'), :<, pr_close.index('await pauseIssue(')

    # Lease reconciliation.
    lease = scheduler[/const openReservationBlockers = reservationBlockers\.filter\(.*?\n            \}/m]
    refute_nil lease, 'the native-blocker reservation release is gone'
    assert_includes lease, 'await removeLabel(issueNumber, inProgressLabel);'
    assert_includes lease, ': blocked by '

    # An active OpenCode run is authoritative too, or the lease would release
    # an issue GitHub is still implementing.
    assert_includes scheduler, 'const workflowRuns = await github.paginate('
    assert_includes scheduler, "run.event !== 'issue_comment'"
    assert_includes scheduler, 'run.path === opencodeWorkflowPath ||'
    assert_includes scheduler, 'run.name === opencodeWorkflowName'
    assert_includes scheduler, "if (\n                openPrIssues.has(issueNumber) ||\n                activeRunIssues.has(issueNumber)\n              ) continue;"
    assert_includes scheduler, "OPENCODE_WORKFLOW_NAME: ${{ inputs.opencode_workflow_name || vars.CONTINUUM_OPENCODE_WORKFLOW_NAME || 'OpenCode agent' }}"
    assert_includes scheduler, "OPENCODE_WORKFLOW_PATH: ${{ inputs.opencode_workflow_path || vars.CONTINUUM_OPENCODE_WORKFLOW_PATH || '.github/workflows/continuum-opencode.yml' }}"
  end

  # The `P[0-2]:` title migration is a one-time backlog conversion. It must keep
  # the digit-class regexp and the title rewrite together: a mutation that
  # strips the label but keeps the rename (or vice versa) silently corrupts the
  # backlog.
  def test_scheduler_priority_title_migration_is_intact
    scheduler = workflow_body('issue-scheduler.yml')
    migration = scheduler[/One-time migration.*?\n            \}/m]
    refute_nil migration, 'the P[0-2]: title migration block is gone'
    assert_includes migration, 'if (currentPriorities.length !== 0) continue;'
    assert_includes migration, 'issue.title.match(/^P([0-2]):\s+/i)'
    assert_includes migration, "const priority = ('priority:p' + match[1]).toLowerCase();"
    assert_includes migration, 'await addLabel(issue.number, priority);'
    assert_includes migration, 'issue.title.replace(/^P[0-2]:\s+/i, \'\')'
    assert_includes migration, 'title: cleanTitle,'
  end

  # The scheduler must be the one place that reserves the owner command, and it
  # must only fire on the owner's own comment — a stranger's `/oc` must not
  # consume WIP capacity.
  def test_scheduler_stub_triggers_the_owner_command_path
    stub = yaml(File.join(ROOT, '.github/caller-stubs/issue-scheduler.yml'))
    assert_equal ['created'], events(stub).fetch('issue_comment').fetch('types')
    gate = stub.fetch('jobs').fetch('call').fetch('if')
    assert_includes gate, "github.event_name != 'issue_comment'"
    assert_includes gate, 'github.actor == github.repository_owner'
    assert_includes gate, "contains(github.event.comment.body, '/oc')"
  end

  def test_empty_vars_resolve_to_the_same_scheduler_defaults
    scheduler = workflow_body('issue-scheduler.yml')
    {
      'DISPATCH_MARKER' => ['dispatch_marker', '<!-- issue-scheduler-dispatch -->', 'AUTOMATION_DISPATCH_MARKER'],
      'IN_PROGRESS_LABEL' => ['in_progress_label', 'automation:in-progress', 'AUTOMATION_IN_PROGRESS_LABEL'],
      'PAUSE_LABEL' => ['pause_marker', 'automation:paused', 'AUTOMATION_PAUSE_LABEL'],
      'WIP_LIMIT' => ['wip_limit', '2', 'AUTOMATION_WIP_LIMIT'],
      'LEASE_MINUTES' => ['lease_minutes', '45', 'AUTOMATION_LEASE_MINUTES'],
      'MAX_DISPATCH_ATTEMPTS' => ['max_dispatch_attempts', '2', 'AUTOMATION_MAX_DISPATCH_ATTEMPTS'],
      'REQUIRE_PRIORITY_LABEL' => ['require_priority_label', 'false', 'AUTOMATION_REQUIRE_PRIORITY_LABEL'],
      'COMMAND_GRACE_MINUTES' => ['command_grace_minutes', '5', 'AUTOMATION_COMMAND_GRACE_MINUTES'],
      'CHILD_OWNED_MARKER' => ['child_owned_marker', '<!-- continuum-child-owned -->', 'CONTINUUM_CHILD_OWNED_MARKER'],
      'LEGACY_CHILD_OWNED_MARKER' => ['legacy_child_owned_marker', '<!-- runtime-worker-owned -->', 'CONTINUUM_LEGACY_CHILD_OWNED_MARKER'],
      'OPENCODE_WORKFLOW_NAME' => ['opencode_workflow_name', 'OpenCode agent', 'CONTINUUM_OPENCODE_WORKFLOW_NAME'],
      'OPENCODE_WORKFLOW_PATH' => ['opencode_workflow_path', '.github/workflows/continuum-opencode.yml', 'CONTINUUM_OPENCODE_WORKFLOW_PATH']
    }.each do |key, (input, expected, variable)|
      line = scheduler.lines.find { |candidate| candidate.include?("#{key}: ${{") }
      refute_nil line, "#{key}: no env mapping found in issue-scheduler.yml"

      expression = line[/\$\{\{(.*)\}\}/, 1].strip
      # Every term of the chain, in order: the input, then the repository
      # variable, then the literal default.
      terms = expression.split('||').map(&:strip)
      assert_equal "inputs.#{input}", terms.first, "#{key}: #{expression}"
      assert_equal "vars.#{variable}", terms[1], "#{key}: the vars. fallback is missing"

      resolve = lambda do |inputs_value, vars_value|
        terms.reduce(nil) do |acc, term|
          acc || case term
                 when /\A'([^']*)'\z/ then Regexp.last_match(1)
                 when /\Avars\./ then vars_value
                 when /\Ainputs\./ then inputs_value
                 end
        end
      end
      assert_equal expected, resolve.call(nil, nil),
                   "#{key}: empty inputs and vars must still yield #{expected.inspect}"
      # A repository that sets the variable must win over the literal default.
      refute_equal expected, resolve.call(nil, 'consumer-value'),
                   "#{key}: a set vars. value must not be overridden by the default"
    end
  end

  # The watchdog recovers a *completed* failed run. On `requested` it would
  # wake on every in-flight run before any conclusion exists, and a run that
  # later succeeds would have already been counted as a recovery. Pin the type
  # list so a widening of it is a deliberate edit.
  def test_watchdog_stub_watches_only_completed_runs
    watched = events(watchdog_stub).fetch('workflow_run')
    assert_equal ['completed'], watched.fetch('types'),
                 'the watchdog must react to completed runs only — ' \
                 '`requested` would count a still-running workflow as a failure'
    assert_equal ['OpenCode agent'], watched.fetch('workflows')
  end

  # ------------------------------------------- add-review-label / CodeRabbit

  def add_review_label_body
    File.read(File.join(ROOT, '.github/workflows/add-review-label.yml'))
  end

  # The same optional-integration contract auto-merge.yml already honours.
  # add-review-label.yml is the *other* half of the CodeRabbit path: it writes
  # the two queue labels and dispatches the retry controller. A repository that
  # never asked for CodeRabbit would get `review-ready` and
  # `coderabbit-review-requested` on every green PR, and a 404 from a
  # `continuum-coderabbit-retry.yml` it does not have.
  def test_add_review_label_gates_the_coderabbit_path_behind_the_flag
    inputs = events(yaml(File.join(ROOT, '.github/workflows/add-review-label.yml')))
             .fetch('workflow_call').fetch('inputs')
    knob = inputs.fetch('require_coderabbit')
    assert_equal '', knob.fetch('default'),
                 'require_coderabbit must default to empty so vars.CONTINUUM_REQUIRE_CODERABBIT decides'
    assert_equal 'string', knob.fetch('type')
    assert_equal false, knob.fetch('required')

    body = add_review_label_body
    assert_includes body, "REQUIRE_CODERABBIT: ${{ inputs.require_coderabbit || vars.CONTINUUM_REQUIRE_CODERABBIT || 'false' }}"
    refute_includes body, "vars.CONTINUUM_REQUIRE_CODERABBIT || 'true'",
                    'CodeRabbit must not default to on'
    assert_match(/const REQUIRE_CODERABBIT =\s*String\(process\.env\.REQUIRE_CODERABBIT \|\| ''\)\.trim\(\)\.toLowerCase\(\);/, body)
    assert_match(/REQUIRE_CODERABBIT === 'true' \|\| REQUIRE_CODERABBIT === '1'/, body)
  end

  # Both CodeRabbit label writes and the retry-controller dispatch must sit
  # behind the flag. Asserting only the env string would let every gate keep
  # reading the flag as `true` and no test would notice, so the writes are
  # checked structurally, by the indentation of their enclosing `if (`.
  def test_add_review_label_writes_no_coderabbit_label_and_dispatches_none_when_disabled
    body = add_review_label_body
    lines = body.lines

    # Every CodeRabbit write site, matched on the API *argument* rather than on
    # the label name alone: the workflow's own prose mentions both labels in
    # the `require_coderabbit` description, and that is not a write.
    #   * createLabel  → name: REQUESTED_LABEL
    #   * addLabels    → labels: ['review-ready'] / labels: [REQUESTED_LABEL]
    label_writes = lines.each_index.select do |index|
      line = lines[index]
      line.match?(/^\s*(name: REQUESTED_LABEL,|labels: \['review-ready'\],|labels: \[REQUESTED_LABEL\],)\s*$/)
    end
    assert_equal 3, label_writes.size,
                 'expected the lock-label create and both label adds — check this test still describes them'

    label_writes.each do |index|
      guards = enclosing_gates(lines, index)
      refute_empty guards, "the CodeRabbit label write at #{index + 1} has no enclosing gate"
      assert guards.any? { |guard| guard.include?('requireCodeRabbit') },
             "the CodeRabbit label write at #{index + 1} is not gated on the flag " \
             "(enclosing gates: #{guards.map(&:strip).join(' | ')})"
    end

    # The dispatch. Exactly one site, and its own condition reads the flag —
    # `queuedForCodeRabbit` alone would skip it but would not prove the gate
    # exists at all.
    assert_equal 1, body.scan("workflow_id: 'continuum-coderabbit-retry.yml'").size,
                 'the CodeRabbit retry dispatch site changed shape'
    # Assert against a small window around the dispatch rather than the whole
    # embedded script: matching `body` makes a failure print all of it.
    lines = body.lines
    at = lines.index { |line| line.include?("workflow_id: 'continuum-coderabbit-retry.yml'") }
    window = lines[[at - 8, 0].max..at].join
    assert_match(/if \(requireCodeRabbit && queuedForCodeRabbit\) \{\s*\n\s*await github\.rest\.actions\.createWorkflowDispatch\(\{/, window,
                 'the CodeRabbit retry dispatch must be gated on the flag')

    # The CI-driven auto-merge wake-up is NOT part of the CodeRabbit path and
    # must keep firing for a repository that disabled CodeRabbit.
    assert_match(/if \(ours\.length > 0\) \{\s*\n\s*await github\.rest\.actions\.createWorkflowDispatch\(\{\s*\n\s*owner,\s*\n\s*repo,\s*\n\s*workflow_id: 'continuum-auto-merge\.yml'/, body,
                 'the auto-merge wake-up must stay unconditional')
  end

  # No core workflow may dispatch a CodeRabbit controller unconditionally. This
  # is the file-level version of the two tests above, so a third workflow
  # acquiring an ungated `continuum-coderabbit-*.yml` dispatch fails here rather
  # than having to be caught one workflow at a time.
  def test_no_core_workflow_dispatches_a_coderabbit_controller_unconditionally
    core = WORKFLOWS.reject { |path| File.basename(path).start_with?('continuum-tech-') }
    sites = core.flat_map do |path|
      lines = File.read(path).lines
      lines.each_index
           .select { |index| lines[index].include?("workflow_id: 'continuum-coderabbit-") }
           .map { |index| [File.basename(path), index, lines] }
    end
    refute_empty sites, 'no core workflow dispatches a CodeRabbit controller at all'

    sites.each do |base, index, lines|
      guards = enclosing_gates(lines, index)
      refute_empty guards, "#{base}:#{index + 1}: CodeRabbit dispatch has no enclosing gate"
      assert guards.any? { |guard| guard.include?('requireCodeRabbit') },
             "#{base}:#{index + 1}: dispatches a CodeRabbit controller without reading the flag " \
             "(enclosing gates: #{guards.map(&:strip).join(' | ')})"
    end
  end

  # ------------------------------------------------- auto-merge / CodeRabbit

  def auto_merge_body
    File.read(File.join(ROOT, '.github/workflows/auto-merge.yml'))
  end

  # CodeRabbit is an OPTIONAL integration, so Continuum's default is off. A
  # `true` default "so current consumers do not change" would make every
  # project without CodeRabbit wait forever for an approval nobody will give,
  # and would dispatch a workflow it does not have. The enabling value belongs
  # in the one repository that asked for CodeRabbit.
  def test_coderabbit_gate_is_off_by_default_and_variable_driven
    inputs = events(yaml(File.join(ROOT, '.github/workflows/auto-merge.yml')))
             .fetch('workflow_call').fetch('inputs')
    knob = inputs.fetch('require_coderabbit')
    assert_equal '', knob.fetch('default'),
                 'require_coderabbit must default to empty so vars.CONTINUUM_REQUIRE_CODERABBIT decides'
    assert_equal 'string', knob.fetch('type')
    assert_equal false, knob.fetch('required')

    body = auto_merge_body
    assert_includes body, "REQUIRE_CODERABBIT: ${{ inputs.require_coderabbit || vars.CONTINUUM_REQUIRE_CODERABBIT || 'false' }}"
    refute_includes body, "vars.CONTINUUM_REQUIRE_CODERABBIT || 'true'",
                    'CodeRabbit must not default to on'
  end

  # Both branches of the flag, checked in the body that acts on them. Asserting
  # only the env string would let every gate keep reading the flag as `true`
  # and no test would notice.
  def test_auto_merge_skips_every_coderabbit_gate_when_disabled
    body = auto_merge_body

    # The flag is parsed once, and the only truthy spellings are explicit.
    assert_match(/const REQUIRE_CODERABBIT =\s*String\(process\.env\.REQUIRE_CODERABBIT \|\| ''\)\.trim\(\)\.toLowerCase\(\);/, body)
    assert_match(/REQUIRE_CODERABBIT === 'true' \|\| REQUIRE_CODERABBIT === '1'/, body)

    # Every CodeRabbit query that decides whether a PR merges is conditional,
    # so a repository without CodeRabbit never waits on one. Each call site is
    # asserted by its own `requireCodeRabbit ? … : …` shape.
    [
      /const reviewBasis = requireCodeRabbit\s*\n\s*\? await codeRabbitReviewBasis\(pr, pr\.head\.sha\)\s*\n\s*: null;/,
      /const rabbitStatus = requireCodeRabbit\s*\n\s*\? await latestCodeRabbitStatus\(pr\.head\.sha\)\s*\n\s*: null;/,
      /const unresolvedThreads = requireCodeRabbit\s*\n\s*\? await unresolvedCodeRabbitThreads\(pr\)\s*\n\s*: \[\];/,
      /const currentHeadNitpicks = requireCodeRabbit\s*\n\s*\? await codeRabbitNitpickReviews\(pr\)\s*\n\s*: \[\];/
    ].each do |shape|
      assert_match shape, body, "an unguarded CodeRabbit query is left in the merge loop"
    end

    # The dispatch is the failure mode that matters: with the flag off, a
    # repository without `continuum-coderabbit-retry.yml` gets a 404 and a PR
    # that never merges. Both dispatch sites must sit inside a gate that reads
    # the flag. Take the guard text between the previous dispatch and this one,
    # up to the enclosing `if`, and require the flag there.
    dispatch_count = body.scan("workflow_id: 'continuum-coderabbit-retry.yml'").size
    assert_equal 2, dispatch_count, 'the two CodeRabbit retry dispatch sites changed shape'
    body.lines.each_with_index do |line, index|
      next unless line.include?("workflow_id: 'continuum-coderabbit-retry.yml'")

      # The dispatch is only reachable when the flag is on, so an enclosing gate
      # has to read it.
      guards = enclosing_gates(body.lines, index)
      refute_empty guards,
                   "the CodeRabbit retry dispatch at #{index + 1} has no enclosing gate"
      # The innermost gates may be ordinary de-duplication checks, so the flag
      # must appear in one of the gates that actually decide reachability.
      assert guards.any? { |guard| guard.include?('requireCodeRabbit') },
             "the CodeRabbit retry dispatch at #{index + 1} is not gated on the flag " \
             "(enclosing gates: #{guards.map(&:strip).join(' | ')})"
    end
    assert_match(/requireCodeRabbit &&\s*\n\s*!reviewBasis &&\s*\n\s*rabbitStatus &&/, body,
                 'the rate-limit delegation must be gated on the flag')
    assert_match(/if \(\s*\n\s*requireCodeRabbit &&\s*\n\s*!reviewBasis &&/, body,
                 'the "waiting for completed CodeRabbit review" gate must be gated on the flag')
    assert_match(/if \(requireCodeRabbit && !reviewBasis\) \{\s*\n\s*const decision = await latestCodeRabbitDecision/, body)

    # The final revalidation is the last chance to cancel a merge; its
    # CodeRabbit half must be conditional too, or the merge is cancelled with
    # the flag off no matter what CI says.
    #
    # The explanatory comment inside the condition must not make the assertion
    # miss, so match the CodeRabbit half as an ordered sequence of its terms
    # rather than as one literal stretch.
    assert_match(/requireCodeRabbit &&[\s\S]{0,400}?!\s*finalReviewBasis\s*\|\|\s*finalUnresolvedThreads\.length !== 0\s*\|\|\s*finalCurrentHeadNitpicks\.length !== 0\s*\)\s*\)\s*\)\s*\{/, body)
    assert_match(/const finalReviewBasis = requireCodeRabbit\s*\n\s*\? await codeRabbitReviewBasis/, body)
    assert_match(/const finalUnresolvedThreads = requireCodeRabbit\s*\n\s*\? await unresolvedCodeRabbitThreads/, body)
    assert_match(/const finalCurrentHeadNitpicks = requireCodeRabbit\s*\n\s*\? await codeRabbitNitpickReviews/, body)

    # The merge log must not read `null.sourceSha` when the flag is off.
    assert_match(/requireCodeRabbit\s*\n\s*\? `CodeRabbit approval from \$\{finalReviewBasis\.sourceSha\}`\s*\n\s*: 'CI only; CodeRabbit is disabled/, body)

    # updateFromMain writes a carry marker only from a CodeRabbit review basis.
    # With the flag off there is no approval to carry, so no marker and no
    # GraphQL thread queries.
    assert_match(/const unresolvedBeforeSync = requireCodeRabbit\s*\n\s*\? await unresolvedCodeRabbitThreads\(pr\)\s*\n\s*: \[\];/, body)
    assert_match(/const reviewBasis =\s*\n\s*requireCodeRabbit && unresolvedBeforeSync\.length === 0\s*\n\s*\? await codeRabbitReviewBasis\(pr, oldHead\)\s*\n\s*: null;/, body)
  end

  # With the flag ON the previous behaviour must be exactly preserved: the
  # approval gate, the rate-limit delegation, the thread/nitpick gates and the
  # final revalidation all still run.
  def test_auto_merge_keeps_the_coderabbit_gates_when_enabled
    body = auto_merge_body

    # Every gate the flag guards is reachable again when it is on, because each
    # guard is a conjunction with `requireCodeRabbit` and nothing else replaced
    # the original condition.
    assert_includes body, 'const reviewBasis = requireCodeRabbit'
    assert_includes body, 'const rabbitStatus = requireCodeRabbit'
    assert_includes body, 'const unresolvedThreads = requireCodeRabbit'
    assert_includes body, 'const currentHeadNitpicks = requireCodeRabbit'

    # The `true` spelling must switch every gate on.
    assert_match(/REQUIRE_CODERABBIT === 'true' \|\| REQUIRE_CODERABBIT === '1'/, body)

    # The CI gate stays unconditional: CodeRabbit never replaced it.
    refute_match(/requireCodeRabbit[^;]*!finalCi/, body)
    assert_includes body, '!finalCi ||'
  end

  end
