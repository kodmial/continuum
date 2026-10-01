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

  def events(workflow)
    workflow['on'] || workflow[true]
  end

  # Three dashes after `continuum`: `continuum-tech-<tech>-<name>*.yml`.
  # The `<name>` part may itself be multi-word, so the rule is a `tech`
  # segment plus at least two further segments — not a fixed count.
  def tech_name?(base)
    segments = base.delete_suffix('.yml').split('-')
    base.start_with?('continuum-') && segments[1] == 'tech' && segments.size >= 4
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
  #   1. core   — `continuum-<name>.yml`, exactly two dash-separated segments
  #               after the prefix, e.g. `continuum-auto-merge.yml`. Installed
  #               by the default `core` set; every project needs these.
  #   2. tech   — `continuum-tech-<tech>-<name>.yml`, exactly three dashes,
  #               e.g. `continuum-tech-swift-release.yml`. Installed only by the
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
        elsif base.start_with?('continuum-') && segments.size == 2
          :core
        elsif tech_name?(base)
          :tech
        else
          flunk "#{base}: neither a core file (continuum-<name>.yml, two segments), " \
                'a tech file (continuum-tech-<tech>-<name>.yml, three dashes), ' \
                'nor a listed historical unprefixed core name'
        end
      assert_includes %i[core tech historical_unprefixed_core], category
    end
    unprefixed = WORKFLOWS.map { |path| File.basename(path) }.reject { |base| base.start_with?('continuum-') }
    assert_equal unprefixed.sort, covered.sort, 'the historical unprefixed core list is stale'
    assert_equal 5, WORKFLOWS.count { |path| tech_name?(File.basename(path)) }
  end

  # Three dashes mean "opt-in library". A tech workflow that any other trigger
  # could fire on would let Continuum trigger its own technology library, which
  # is exactly what the split exists to prevent.
  def test_tech_workflows_are_only_reusable_and_never_self_triggered
    assert_equal 5, TECH_STUBS.size, 'tech set size'
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
  # `.github/caller-stubs/` (task-domain layer), `tech` the five
  # `continuum-tech-swift-*` stubs in `.github/caller-stubs/tech/`, and
  # `parent` the four child-execution stubs in `.github/caller-stubs/parent/`.
  CORE_COUNT = 11
  TECH_COUNT = 5
  PARENT_COUNT = 4

  def test_installer_local_and_explicit_ref
    fixture do |dir|
      assert_equal CORE_COUNT, CORE_STUBS.size, 'core stub set drifted'
      output, status = Open3.capture2e('bash', File.join(ROOT, 'install.sh'), File.join(dir, 'local'))
      assert status.success?, output
      assert_equal CORE_COUNT, Dir[File.join(dir, 'local/.github/workflows/*.yml')].size
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
      # `swift` was the old technology-set name. Three dashes now mean "opt-in
      # library", and the value no longer exists.
      output, status = Open3.capture2e('bash', File.join(ROOT, 'install.sh'), File.join(dir, 'legacy'), 'main', 'swift')
      refute status.success?, output
      assert_includes output, 'invalid set: swift'
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

end
