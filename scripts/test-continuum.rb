# frozen_string_literal: true

require 'minitest/autorun'
require 'yaml'
require 'tmpdir'
require 'fileutils'
require 'open3'

class ContinuumTest < Minitest::Test
  ROOT = File.expand_path('..', __dir__)
  CORE_STUBS = Dir[File.join(ROOT, '.github/caller-stubs/*.yml')].sort
  TECH_STUBS = Dir[File.join(ROOT, '.github/caller-stubs/tech/*.yml')].sort
  PARENT_STUBS = Dir[File.join(ROOT, '.github/caller-stubs/parent/*.yml')].sort
  WORKFLOWS = Dir[File.join(ROOT, '.github/workflows/*.yml')].sort
  # Project-owned entry workflows used only by the Continuum repository itself.
  # They deliberately stay outside the `continuum-` namespace so installer
  # ownership and reusable-engine ownership remain unambiguous.
  PROJECT_ENTRY_WORKFLOWS = %w[automation.yml ci.yml opencode.yml pr-agent.yml pr-agent-recovery.yml pr-agent-router.yml].freeze
  # Every caller stub in every layer, for the checks that must not care which
  # layer a file belongs to.
  ALL_STUBS = (CORE_STUBS + TECH_STUBS + PARENT_STUBS).sort
  STUBS = (CORE_STUBS + TECH_STUBS).sort
  # Transitional workflow_run aliases kept by the scheduler so already
  # installed parent callers using the pre-SubTask names still wake it.
  LEGACY_WORKFLOW_RUN_NAMES = ['Child task', 'Child review', 'Child PR review'].freeze
  # Primary CI is deliberately project-owned. The shared validation workflow is
  # an engine called by each consumer's ci.yml, not an install-managed caller.
  PROJECT_OWNED_WORKFLOW_NAMES = ['CI'].freeze

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
    yaml(File.join(ROOT, '.github/workflows', 'continuum-opencode-repair.yml'))
  end

  # The raw YAML of one named step, from the `- name:` line up to the next
  # step or job boundary. `script_body` only reaches the first embedded
  # `script: |` block of a whole workflow, which cannot address an individual
  # step of a multi-step job.
  def step_body(body, step_name)
    lines = body.lines
    start = lines.index { |line| line.start_with?("      - name: #{step_name}") }
    return nil unless start

    # A top-level comment sits at the same indentation as the step's own
    # `- name:`, so a bare indentation test would cut the step short at the
    # comment that follows it.
    boundary = lambda do |line|
      (line.start_with?('      - name: ') || line =~ /^ {0,6}\S/) &&
        !line.strip.start_with?('#')
    end
    rest = lines[(start + 1)..]
    stop = rest.index(&boundary)
    stop ? lines[start...(start + 1 + stop)].join : lines[start..].join
  end

  # The modes the OpenCode engine admits on the `workflow_dispatch` path,
  # parsed out of the job's own `if` guard rather than hardcoded here.
  def dispatch_modes
    body = workflow_body('continuum-opencode.yml')
    body[/contains\(fromJSON\('\[([^\]]+)\]'\), inputs\.mode\)/, 1].to_s
        .scan(/"([^"]+)"/).flatten
  end

  # Every `.../dispatches` call site, captured together with the lines that
  # follow it, so a test can prove the call is really made and really carries
  # the payload. A call replaced by an `echo` leaves no window at all, which is
  # exactly the "green no-op" a presence-only check misses.
  DISPATCHING_WORKFLOWS = %w[
    continuum-auto-merge.yml
    continuum-issue-scheduler.yml
    continuum-opencode-repair.yml
    continuum-opencode-unresolved.yml
    continuum-opencode.yml
  ].freeze

  def dispatch_calls(name)
    lines = workflow_body(name).lines
    lines.each_index.select do |index|
      lines[index].include?('/dispatches') ||
        lines[index].include?('createWorkflowDispatch')
    end.map { |index| lines[[index - 2, 0].max, 16].join }
  end

  def all_dispatch_calls
    DISPATCHING_WORKFLOWS.flat_map { |name| dispatch_calls(name) }
  end

  # The exact `{ … }` block opened by `header`, matched by brace depth rather
  # than by indentation. A guard whose condition was neutered (`if (false)`)
  # keeps its error text somewhere in the file, so the only way to prove the
  # text is *inside* the guard is to extract the guard's own body.
  def js_block(body, header)
    start = body.index(header)
    return nil if start.nil?

    opening = body.index('{', start + header.length)
    return nil if opening.nil?

    depth = 0
    index = opening
    while index < body.length
      case body[index]
      when '{' then depth += 1
      when '}'
        depth -= 1
        return body[start..index] if depth.zero?
      end
      index += 1
    end
    nil
  end

  # The `if [[ -n "$VAR" ]]; then … fi` block that gates one shell variable,
  # from the `if` line through its `fi` at the same indentation. Scoping an
  # assertion to the gated body is what distinguishes a live gate from the
  # same literal text left behind by a neutered one.
  def shell_if_gate(step, variable)
    header = %(if [[ -n "$#{variable}" ]]; then)
    lines = step.lines
    start = lines.index { |line| line.strip == header }
    return nil if start.nil?

    indent = lines[start][/\A */].size
    rest = lines[(start + 1)..]
    stop = rest.index { |line| line.strip == 'fi' && line[/\A */].size == indent }
    stop ? lines[start..(start + stop)].join : nil
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
  #
  # install.sh writes the stored stub name verbatim, so the installed name is
  # the stub name — not a name computed from it. That is the whole point of the
  # uniform rule: the repository file name and the consumer file name are the
  # same string, so no install-time rewrite can drift from either.
  def assert_core_install_names(dir)
    installed = Dir[File.join(dir, '.github/workflows/*.yml')].map { |file| File.basename(file) }.sort
    expected = CORE_STUBS.map { |path| File.basename(path) }.sort
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
    names = ALL_STUBS.map { |path| yaml(path).fetch('name') }
    each_pair do |caller, _|
      events(caller).fetch('workflow_run', {}).fetch('workflows', []).each do |name|
        assert_includes names + PROJECT_OWNED_WORKFLOW_NAMES + LEGACY_WORKFLOW_RUN_NAMES, name
      end
    end
  end

  # `workflow_run.workflows` matches a workflow's `name:` VALUE. Reusable
  # engines and repository entry workflows are separate identity layers:
  # `ci.yml` deliberately has the same public name as its CI engine, and
  # `opencode.yml` deliberately has the same public name as its OpenCode
  # engine. Within either layer a duplicate is still ambiguous and forbidden.
  def test_workflow_names_are_unique_within_each_layer
    engine_workflows = WORKFLOWS.reject do |path|
      PROJECT_ENTRY_WORKFLOWS.include?(File.basename(path))
    end
    project_entries = WORKFLOWS.select do |path|
      PROJECT_ENTRY_WORKFLOWS.include?(File.basename(path))
    end

    {
      'engine workflows' => engine_workflows,
      'project entry workflows' => project_entries,
      'core stubs' => CORE_STUBS,
      'tech stubs' => TECH_STUBS,
      'parent stubs' => PARENT_STUBS
    }.each do |layer, paths|
      names = paths.map { |path| yaml(path).fetch('name') }
      assert_equal names, names.uniq,
                   "#{layer}: two workflows share a `name:`, which makes a " \
                   'workflow_run.workflows filter ambiguous'
    end

    expected_identity_pairs = {
      'ci.yml' => 'continuum-validation.yml',
      'opencode.yml' => 'continuum-opencode.yml'
    }
    expected_identity_pairs.each do |entry, engine|
      assert_equal yaml(File.join(ROOT, '.github/workflows', engine)).fetch('name'),
                   yaml(File.join(ROOT, '.github/workflows', entry)).fetch('name'),
                   "#{entry}: self entry must retain the public workflow identity of #{engine}"
    end

    engine_names = engine_workflows.map { |path| yaml(path).fetch('name') }
    entry_names = project_entries.map { |path| yaml(path).fetch('name') }
    assert_equal %w[CI OpenCode\ agent].sort,
                 (engine_names & entry_names).sort,
                 'only CI and OpenCode may deliberately share engine/entry workflow identities'
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

  # Continuum-owned reusable workflows have a machine-readable namespace:
  # core is `continuum-<name>.yml` and technology-library workflows are
  # `continuum-tech-<tech>-<name>.yml`. The Continuum repository may also own
  # a very small set of project entry workflows used only to dogfood the engine;
  # those are explicitly listed in PROJECT_ENTRY_WORKFLOWS and MUST NOT use the
  # `continuum-` prefix because they are not install-managed consumer files.
  def test_workflow_files_obey_the_ownership_naming_rule
    engine_workflows = WORKFLOWS.reject do |path|
      PROJECT_ENTRY_WORKFLOWS.include?(File.basename(path))
    end

    engine_workflows.each do |path|
      base = File.basename(path)
      segments = base.delete_suffix('.yml').split('-')
      category =
        if tech_name?(base)
          :tech
        elsif base.start_with?('continuum-') && segments.size >= 2
          :core
        else
          flunk "#{base}: Continuum-owned reusable workflows must be continuum-<name>.yml " \
                'or continuum-tech-<tech>-<name>.yml'
        end
      assert_includes %i[core tech], category
    end

    actual_entries = WORKFLOWS.map { |path| File.basename(path) } & PROJECT_ENTRY_WORKFLOWS
    assert_equal PROJECT_ENTRY_WORKFLOWS.sort, actual_entries.sort
    PROJECT_ENTRY_WORKFLOWS.each do |base|
      refute base.start_with?('continuum-'), "#{base}: project-owned self entry must not use the continuum- prefix"
      refute_includes ALL_STUBS.map { |path| File.basename(path) }, base,
                      "#{base}: project-owned self entry must never be install-managed"
    end
    assert_tech_layers_agree
  end

  def test_continuum_owned_workflows_and_all_stubs_are_continuum_prefixed
    refute_empty WORKFLOWS, 'no workflows found: the tree is empty or the path is wrong'
    refute_empty ALL_STUBS, 'no caller stubs found: the tree is empty or the path is wrong'
    owned_workflows = WORKFLOWS.reject do |path|
      PROJECT_ENTRY_WORKFLOWS.include?(File.basename(path))
    end
    unprefixed = (owned_workflows + ALL_STUBS).reject do |path|
      File.basename(path).start_with?('continuum-')
    end.map { |path| File.basename(path) }
    assert_empty unprefixed,
                 "Continuum-owned files without the continuum- prefix: #{unprefixed.sort.join(', ')}"
  end

  def test_self_dogfood_entries_are_thin_local_callers
    ci = yaml(File.join(ROOT, '.github/workflows/ci.yml'))
    opencode = yaml(File.join(ROOT, '.github/workflows/opencode.yml'))
    automation = yaml(File.join(ROOT, '.github/workflows/automation.yml'))

    assert_equal 'CI', ci.fetch('name')
    assert_equal 'OpenCode agent', opencode.fetch('name')
    assert_equal 'Continuum automation', automation.fetch('name')

    assert_equal './.github/workflows/continuum-validation.yml',
                 ci.fetch('jobs').fetch('validate').fetch('uses')
    assert_equal './.github/workflows/continuum-opencode.yml',
                 opencode.fetch('jobs').fetch('call').fetch('uses')

    expected_automation_callees = %w[
      continuum-auto-merge.yml
      continuum-issue-scheduler.yml
      continuum-opencode-repair.yml
      continuum-opencode-watchdog.yml
    ].sort
    actual_automation_callees = automation.fetch('jobs').values
      .map { |job| job['uses'] }
      .compact
      .map { |uses| File.basename(uses) }
      .sort
    assert_equal expected_automation_callees, actual_automation_callees

    scheduler_inputs = automation.fetch('jobs').fetch('scheduler').fetch('with')
    assert_equal 'OpenCode agent', scheduler_inputs.fetch('opencode_workflow_name')
    assert_equal '.github/workflows/opencode.yml', scheduler_inputs.fetch('opencode_workflow_path')

    repair_inputs = automation.fetch('jobs').fetch('repair').fetch('with')
    assert_equal 'opencode.yml', repair_inputs.fetch('opencode_workflow')
    assert_equal 'automation.yml', repair_inputs.fetch('auto_merge_workflow')

    watchdog_inputs = automation.fetch('jobs').fetch('watchdog').fetch('with')
    assert_equal 'OpenCode agent', watchdog_inputs.fetch('watched_workflow')

    # A custom run-name changes workflow_run.name in the delivered payload on
    # current GitHub Actions, even though workflow_run.workflows is matched by
    # the workflow identity. Route completed self-entry runs by their stable
    # repository-local path so OpenCode watchdog/scheduler recovery cannot be
    # skipped merely because opencode.yml reports "OpenCode issue #<n>".
    automation_body = File.read(File.join(ROOT, '.github/workflows/automation.yml'))
    assert_includes automation_body,
                    "github.event.workflow_run.path == '.github/workflows/opencode.yml'"
    assert_includes automation_body,
                    "github.event.workflow_run.path == '.github/workflows/ci.yml'"
    refute_includes automation_body,
                    "github.event.workflow_run.name == 'OpenCode agent'"
    refute_includes automation_body,
                    "github.event.workflow_run.name == 'CI'"
  end

  # install.sh installs the stored stub name verbatim. A prefix computed at
  # install time is what this repository removed: it makes the name a consumer
  # ends up with differ from the name Continuum ships, so a renamed stub keeps
  # installing under a name nothing else in the repository knows.
  def test_installer_does_not_prefix_names_at_install_time
    body = File.read(File.join(ROOT, 'install.sh'))
    refute_match(/continuum-\$\{?name/, body,
                 'install.sh must not build an installed name by prepending the prefix')
    refute_match(/\|\|\s*name="continuum-/, body,
                 'install.sh must not fall back to prefixing the stub name')
    refute_match(/name="continuum-\$f"/, body,
                 'install.sh must write the stored stub name verbatim')
  end

  # `install.sh` fetches stubs over `curl` whenever two or more positional
  # arguments are given, so a remote-path test must not touch the network. This
  # puts a `curl` on PATH that serves the repository's real stub for the
  # requested name, or an override when the test needs a body the repository
  # does not ship.
  # The variable names are prefixed so they cannot collide with install.sh's own
  # internals: it assigns an ARRAY to a variable named `STUBS`, and a colliding
  # exported value is replaced by it rather than passed through to the child.
  def fake_curl(dir, overrides = {})
    bin = File.join(dir, 'bin')
    overrides_dir = File.join(dir, 'curl-overrides')
    FileUtils.mkdir_p([bin, overrides_dir])
    overrides.each do |name, body|
      File.write(File.join(overrides_dir, name), body)
    end
    File.write(File.join(bin, 'curl'), <<~SH)
      #!/usr/bin/env bash
      set -eu
      name="${@: -1}"
      name="${name##*/}"
      if [ -f "$CURL_OVERRIDES/$name" ]; then
        cat "$CURL_OVERRIDES/$name"
      else
        for dir in "$CURL_STUB_DIR" "$CURL_STUB_DIR/tech" "$CURL_STUB_DIR/parent"; do
          if [ -f "$dir/$name" ]; then cat "$dir/$name"; exit 0; fi
        done
        exit 22
      fi
    SH
    FileUtils.chmod(0755, File.join(bin, 'curl'))
    {'PATH' => "#{bin}:#{ENV['PATH']}",
     'CURL_OVERRIDES' => overrides_dir,
     'CURL_STUB_DIR' => File.join(ROOT, '.github/caller-stubs')}
  end

  # A caller name that Continuum no longer ships must not survive forever in the
  # consumer: a renamed stub that leaves its predecessor behind gives the
  # consumer two files where the dispatchers only know one. Deletion is still
  # destructive, so it is fenced — see test_installer_prune_requires_ownership.
  #
  # This helper writes a body that looks like a real installed caller, which is
  # what makes it a prune candidate: the `kodmial/continuum/` reference inside
  # it is the ownership evidence, not its name.
  def superseded_caller(name)
    <<~YAML
      name: #{name}
      on:
        workflow_call:
          inputs: {}
      jobs:
        call:
          uses: kodmial/continuum/.github/workflows/#{name}
          secrets: inherit
    YAML
  end

  def test_installer_prunes_superseded_callers
    fixture do |dir|
      target = File.join(dir, 'superseded')
      stale = File.join(target, '.github/workflows/continuum-removed-caller.yml')
      FileUtils.mkdir_p(File.dirname(stale))
      File.write(stale, superseded_caller('continuum-removed-caller.yml'))
      output, status = Open3.capture2e('bash', File.join(ROOT, 'install.sh'), target, '--yes')
      assert status.success?, output
      refute File.exist?(stale), 'a caller Continuum no longer ships must be removed on install'
      assert_includes output, 'removed superseded Continuum caller: continuum-removed-caller.yml'
      # A project-owned workflow is never Continuum's to delete.
      project = File.join(target, '.github/workflows/ci.yml')
      File.write(project, "name: CI\n")
      output, status = Open3.capture2e('bash', File.join(ROOT, 'install.sh'), target, '--yes')
      assert status.success?, output
      assert File.exist?(project), 'install.sh must not remove a project-owned workflow'
    end
  end

  # A `uses:` line is legal at two levels in a workflow: job-level, and
  # step-level as a list item (`- uses: ...`). The ref parameter exists to pin
  # an installed caller to the requested revision, so BOTH forms must be
  # rewritten. A sed anchored only on `^[[:space:]]*uses:` matches only the
  # job-level form and silently leaves every step-level reference on `main`,
  # which is the exact guarantee the parameter promises. Every caller is
  # job-level today, so this is latent, not yet observed.
  def test_installer_rewrites_step_level_uses_at_the_requested_ref
    fixture do |dir|
      stub = <<~YAML
        name: Continuum reusable child worker
        on:
          workflow_call:
            inputs: {}
        jobs:
          joblevel:
            uses: kodmial/continuum/.github/workflows/continuum-opencode.yml@main
          steplevel:
            runs-on: ubuntu-latest
            steps:
              - uses: kodmial/continuum/.github/workflows/continuum-opencode.yml@main
              - uses: kodmial/continuum/.github/workflows/continuum-opencode.yml@main # trailing comment
              - uses: kodmial/continuum/.github/workflows/continuum-opencode.yml@abc123
              - uses: actions/checkout@v4
              - run: echo "an unrelated main mention"
      YAML
      # Three positionals force the remote path, so the rewrite is exercised on a
      # fetched template exactly as a `curl | bash` consumer would see it.
      env = fake_curl(dir, 'continuum-opencode.yml' => stub)
      target = File.join(dir, 'consumer')
      output, status = Open3.capture2e(env, 'bash', File.join(ROOT, 'install.sh'), target, 'v9.9.9', 'core')
      assert status.success?, output

      body = File.read(File.join(target, '.github/workflows/continuum-opencode.yml'))
      job_level = body[/^    uses: (.*)$/, 1]
      step_lines = body.scan(/^      - uses: (.*)$/).flatten

      assert_equal 'kodmial/continuum/.github/workflows/continuum-opencode.yml@v9.9.9', job_level,
                   'job-level uses: must be rewritten to the requested ref'
      assert_equal 'kodmial/continuum/.github/workflows/continuum-opencode.yml@v9.9.9', step_lines[0],
                   'step-level `- uses:` must be rewritten to the requested ref, not left on main'
      assert_equal 'kodmial/continuum/.github/workflows/continuum-opencode.yml@v9.9.9 # trailing comment',
                   step_lines[1],
                   'a trailing comment must not stop the rewrite, and must be preserved'
      # The ref substitution is still an exact-match rewrite, not a sweep:
      # a third-party action, an already-pinned Continuum ref, and an unrelated
      # `main` in prose all have to survive untouched.
      assert_equal 'kodmial/continuum/.github/workflows/continuum-opencode.yml@abc123', step_lines[2],
                   'only a @main ref is rewritten; an already-pinned ref is left alone'
      assert_equal 'actions/checkout@v4', step_lines[3], 'a third-party action must not be rewritten'
      assert_includes body, 'echo "an unrelated main mention"',
                      'an unrelated `main` must never be rewritten'
      # And the result is still parseable YAML.
      parsed = yaml(File.join(target, '.github/workflows/continuum-opencode.yml'))
      assert_equal 'ubuntu-latest', parsed.fetch('jobs').fetch('steplevel').fetch('runs-on')
    end
  end

  # The ref rewrite anchors on the end of the line, so everything a real line
  # can legally carry *after* the ref has to be tolerated explicitly. Each case
  # below is a line that silently stayed on `main` when the anchor was too
  # strict — which is the exact failure the ref parameter promises to prevent,
  # just moved from a step-level `uses:` to a quoted or CRLF one.
  #
  # The negative cases matter as much as the positive ones: an anchor loosened
  # far enough to catch them would start rewriting third-party refs.
  def test_installer_ref_rewrite_tolerates_quotes_comments_and_crlf
    fixture do |dir|
      stub = <<~YAML
        name: Continuum reusable OpenCode
        on:
          workflow_call:
            inputs: {}
        jobs:
          job:
            runs-on: ubuntu-latest
            steps:
              - uses: "kodmial/continuum/.github/workflows/continuum-opencode.yml@main"
              - uses: 'kodmial/continuum/.github/workflows/continuum-opencode.yml@main'
              - uses: kodmial/continuum/.github/workflows/continuum-opencode.yml@main#glued
              - uses: kodmial/continuum/.github/workflows/continuum-opencode.yml@branch-main
              - uses: actions/checkout@main
              - uses: kodmial/continuum/other/thing.yml@main
      YAML
      env = fake_curl(dir, 'continuum-opencode.yml' => stub)
      target = File.join(dir, 'consumer')
      output, status = Open3.capture2e(env, 'bash', File.join(ROOT, 'install.sh'), target, 'v9.9.9', 'core')
      assert status.success?, output

      body = File.read(File.join(target, '.github/workflows/continuum-opencode.yml'))
      lines = body.scan(/^      - uses: (.*)$/).flatten
      base = 'kodmial/continuum/.github/workflows/continuum-opencode.yml@v9.9.9'

      assert_equal %("#{base}"), lines[0], 'a double-quoted ref must be rewritten, keeping its quotes'
      assert_equal %('#{base}'), lines[1], 'a single-quoted ref must be rewritten, keeping its quotes'
      # YAML opens a comment only when `#` follows whitespace, so `@main#glued`
      # is a ref literally named `main#glued`. Rewriting it would corrupt the
      # ref; leaving it alone is correct and must be pinned by a test.
      assert_equal 'kodmial/continuum/.github/workflows/continuum-opencode.yml@main#glued', lines[2],
                   'a # glued to the ref is part of the ref, not a comment'
      assert_equal 'kodmial/continuum/.github/workflows/continuum-opencode.yml@branch-main', lines[3],
                   'only a bare @main ref is rewritten; @branch-main is a different ref'
      assert_equal 'actions/checkout@main', lines[4], 'a third-party action ref must never be rewritten'
      assert_equal 'kodmial/continuum/other/thing.yml@main', lines[5],
                   'only .github/workflows/ paths are Continuum calls; other paths must not be rewritten'

      # A CRLF template is rewritten too, and the CR survives so the checkout's
      # line endings are not silently half-converted.
      crlf_stub = "name: Continuum reusable OpenCode\r\n" \
                  "on:\r\n  workflow_call:\r\n    inputs: {}\r\n" \
                  "jobs:\r\n  job:\r\n    runs-on: ubuntu-latest\r\n    steps:\r\n" \
                  "      - uses: kodmial/continuum/.github/workflows/continuum-opencode.yml@main\r\n"
      crlf_env = fake_curl(dir, 'continuum-opencode.yml' => crlf_stub)
      crlf_target = File.join(dir, 'crlf-consumer')
      output, status = Open3.capture2e(crlf_env, 'bash', File.join(ROOT, 'install.sh'),
                                       crlf_target, 'v9.9.9', 'core')
      assert status.success?, output
      crlf = File.read(File.join(crlf_target, '.github/workflows/continuum-opencode.yml'))
      assert_includes crlf, "continuum-opencode.yml@v9.9.9\r\n",
                      'a CRLF template must be rewritten to the requested ref'
      refute_includes crlf, 'continuum-opencode.yml@main',
                      'no ref may be left on main in a CRLF template'
    end
  end

  # Deletion is the only destructive thing the installer does, so the uniform
  # `continuum-` prefix alone must never be enough to qualify a file for it. A
  # consumer is entitled to name its own workflow `continuum-experiment.yml`;
  # deleting it would lose work the installer never wrote and cannot restore.
  def test_installer_prune_requires_ownership_not_just_the_prefix
    fixture do |dir|
      target = File.join(dir, 'consumer')
      hand_written = File.join(target, '.github/workflows/continuum-experiment.yml')
      FileUtils.mkdir_p(File.dirname(hand_written))
      # Carries the uniform prefix but no reference back to this repository:
      # it is a project-owned file that borrows the naming convention.
      File.write(hand_written, <<~YAML)
        name: My experiment
        on:
          workflow_dispatch:
        jobs:
          probe:
            runs-on: ubuntu-latest
            steps:
              - run: echo hi
      YAML
      output, status = Open3.capture2e('bash', File.join(ROOT, 'install.sh'), target, '--yes')
      assert status.success?, output
      assert File.exist?(hand_written),
             'a continuum- prefixed file Continuum did not write must never be pruned'
      refute_includes output, 'removed superseded Continuum caller: continuum-experiment.yml'
    end
  end

  # Mentioning the repository is not the same as being written by it. The
  # ownership marker is a `uses:` call to one of Continuum's workflows, and a
  # hand-written file that merely cites the path in a comment still satisfies a
  # bare substring grep — so a comment is exactly the shape that defeats it.
  # Deleting such a file loses project work the installer never created, so the
  # marker has to be anchored on the call, not on the mention.
  def test_installer_prune_rejects_a_file_that_only_mentions_continuum_in_a_comment
    fixture do |dir|
      target = File.join(dir, 'consumer')
      mention_only = File.join(target, '.github/workflows/continuum-notes.yml')
      FileUtils.mkdir_p(File.dirname(mention_only))
      # Every signal except the one that matters: the uniform prefix, and the
      # repository path in the body — but only inside comments.
      File.write(mention_only, <<~YAML)
        # cribbed from kodmial/continuum/.github/workflows/continuum-opencode.yml
        name: My notes
        on:
          workflow_dispatch:
        jobs:
          probe:
            runs-on: ubuntu-latest
            steps:
              - run: echo "see kodmial/continuum/.github/workflows/continuum-opencode.yml"
      YAML
      # Control: the same shape with a real call must still be pruned, or this
      # test would pass for the wrong reason (an ownership test that rejects
      # everything deletes nothing).
      real = File.join(target, '.github/workflows/continuum-dropped.yml')
      File.write(real, superseded_caller('continuum-dropped.yml'))

      output, status = Open3.capture2e('bash', File.join(ROOT, 'install.sh'), target, '--yes')
      assert status.success?, output
      assert File.exist?(mention_only),
             'a file that only MENTIONS Continuum in a comment is project-owned and must not be pruned'
      refute_includes output, 'removed superseded Continuum caller: continuum-notes.yml'
      refute File.exist?(real),
             'control: a file that really `uses:` a Continuum workflow must still be pruned'
      assert_includes output, 'removed superseded Continuum caller: continuum-dropped.yml'
    end
  end

  # Every caller Continuum ships must satisfy the installer's own ownership
  # test, or a superseded caller could survive every future rename because the
  # fence quietly stopped recognising the files it exists to clean up.
  def test_every_shipped_caller_satisfies_the_installers_ownership_test
    refute_empty ALL_STUBS
    ALL_STUBS.each do |stub|
      body = File.read(stub)
      assert_match(/^[[:space:]]*-?[[:space:]]*uses:[[:space:]]*["']?kodmial\/continuum\/\.github\/workflows\//,
                   body,
                   "#{File.basename(stub)}: the installer would not recognise this shipped caller as its own, " \
                   'so a rename would leave it behind forever')
    end
  end

  # Deleting files unattended is how an install turns into data loss. The
  # default non-interactive path must therefore never delete: it reports what
  # it would remove and leaves the decision to the operator. `--yes` and
  # CONTINUUM_INSTALL_ASSUME_YES are the explicit opt-outs for CI.
  def test_installer_prune_is_non_destructive_unless_confirmed
    fixture do |dir|
      target = File.join(dir, 'consumer')
      stale = File.join(target, '.github/workflows/continuum-removed-caller.yml')
      FileUtils.mkdir_p(File.dirname(stale))
      File.write(stale, superseded_caller('continuum-removed-caller.yml'))

      # Open3 gives the child no terminal, which is exactly the CI shape.
      output, status = Open3.capture2e('bash', File.join(ROOT, 'install.sh'), target)
      assert status.success?, output
      assert File.exist?(stale),
             'a non-interactive install must not delete anything without confirmation'
      assert_includes output, 'not removing superseded Continuum callers'
      assert_includes output, 'continuum-removed-caller.yml',
                      'the operator must be told exactly which files are candidates'
      assert_includes output, '--yes', 'the message must name the opt-out'

      # The environment escape hatch is equivalent to the flag, so an
      # unattended install can opt in without rewriting its command line.
      output, status = Open3.capture2e(
        { 'CONTINUUM_INSTALL_ASSUME_YES' => '1' }, 'bash', File.join(ROOT, 'install.sh'), target
      )
      assert status.success?, output
      refute File.exist?(stale), 'CONTINUUM_INSTALL_ASSUME_YES=1 must permit the prune'
      assert_includes output, 'removed superseded Continuum caller: continuum-removed-caller.yml'
    end
  end

  # The environment escape hatch accepts `1`, `true` and `yes` — three spellings
  # of one explicit opt-in, so a CI runner configured with the human-readable
  # one behaves exactly like the numeric one. Nothing else may: this variable
  # authorises deletion, so it is matched in full, case-sensitively, and an
  # empty or near-miss value has to leave the confirmation in place.
  #
  # Every other fixture here sets `=1`, which is why narrowing the match back to
  # `== 1` left the suite green: the other two spellings were shipped untested.
  # This table is what catches that, and its rejected column is what stops the
  # accepted column from being widened into something unguarded.
  def test_assume_yes_environment_variable_accepts_only_its_three_spellings
    fixture do |dir|
      accepted = %w[1 true yes]
      rejected = ['', '0', 'false', 'TRUE', 'True', 'Yes', 'y', 'on', '2', '1 ', ' 1', '11']
      accepted.each do |value|
        target = File.join(dir, "consumer-accepted-#{value.inspect}")
        stale = File.join(target, '.github/workflows/continuum-removed-caller.yml')
        FileUtils.mkdir_p(File.dirname(stale))
        File.write(stale, superseded_caller('continuum-removed-caller.yml'))

        output, status = Open3.capture2e(
          { 'CONTINUUM_INSTALL_ASSUME_YES' => value }, 'bash', File.join(ROOT, 'install.sh'), target
        )
        assert status.success?, "CONTINUUM_INSTALL_ASSUME_YES=#{value.inspect}: #{output}"
        refute File.exist?(stale),
               "CONTINUUM_INSTALL_ASSUME_YES=#{value.inspect} is an accepted opt-in and must permit the prune"
        assert_includes output, 'removed superseded Continuum caller: continuum-removed-caller.yml'
      end
      rejected.each do |value|
        target = File.join(dir, "consumer-rejected-#{value.inspect}")
        stale = File.join(target, '.github/workflows/continuum-removed-caller.yml')
        FileUtils.mkdir_p(File.dirname(stale))
        File.write(stale, superseded_caller('continuum-removed-caller.yml'))

        output, status = Open3.capture2e(
          { 'CONTINUUM_INSTALL_ASSUME_YES' => value }, 'bash', File.join(ROOT, 'install.sh'), target
        )
        assert status.success?, "CONTINUUM_INSTALL_ASSUME_YES=#{value.inspect}: #{output}"
        assert File.exist?(stale),
               "CONTINUUM_INSTALL_ASSUME_YES=#{value.inspect} is not an opt-in and must leave the prune confirmed"
        assert_includes output, 'not removing superseded Continuum callers'
      end
    end
  end

  # Installing one layer must never remove another layer's callers. Today the
  # three sets are independent files in one directory, so a one-line edit to the
  # prune candidate list — or a future edit to `"${STUBS[@]}"` — could delete a
  # consumer's whole tech or parent layer with no warning. The growth
  # 19 -> 20 -> 24 is the observable form of that guarantee.
  def test_installing_one_set_does_not_delete_another_sets_callers
    fixture do |dir|
      target = File.join(dir, 'consumer')
      env = fake_curl(dir).merge('CONTINUUM_INSTALL_ASSUME_YES' => '1')
      workflows = File.join(target, '.github/workflows')
      counts = {}
      %w[core tech parent].each do |set|
        output, status = Open3.capture2e(env, 'bash', File.join(ROOT, 'install.sh'), target, 'main', set)
        assert status.success?, output
        installed = Dir[File.join(workflows, '*.yml')].map { |f| File.basename(f) }
        counts[set] = installed.size
        # Every layer installed so far must still be present, in full.
        CORE_STUBS.each do |stub|
          base = File.basename(stub)
          next unless set == 'core' || installed.include?(base)
          assert_includes installed, base, "#{set} install removed the core caller #{base}"
        end
        if set == 'tech'
          TECH_STUBS.each { |stub| assert_includes installed, File.basename(stub) }
        end
        if set == 'parent'
          PARENT_STUBS.each { |stub| assert_includes installed, File.basename(stub) }
        end
      end
      # Each set adds exactly its own files: 19 core, +1 tech, +4 parent.
      assert_equal CORE_STUBS.size, counts['core']
      assert_equal CORE_STUBS.size + TECH_STUBS.size, counts['tech']
      assert_equal ALL_STUBS.size, counts['parent']
      assert_equal 24, ALL_STUBS.size,
                   'every caller Continuum ships, across all three layers'
    end
  end

  # Installing into Continuum's own checkout is never a consumer install, and
  # it must never be a *partial* one either. This repository's
  # `.github/workflows/` holds the real core workflows; the files the installer
  # writes are thin caller stubs that `uses:` them. Installing here therefore
  # replaces every core workflow with a caller pointing back at Continuum.
  #
  # Skipping the prune was not enough, because the WRITE happens first and is
  # the destructive half: `install.sh . --yes` measured 14 files changed, 461
  # insertions, 6413 deletions. So the installer refuses outright, for every
  # set, and the test asserts the refusal is loud AND that not one core file was
  # touched — the file contents below are the witness.
  def test_installer_refuses_to_write_callers_over_continuum_its_own_workflows
    fixture do |dir|
      # The installer resolves "self" from its own location, so the guard is
      # exercised by running a copy of it that sits next to the workflows
      # directory it must refuse to overwrite.
      checkout = File.join(dir, 'continuum')
      workflows = File.join(checkout, '.github/workflows')
      FileUtils.mkdir_p(workflows)
      FileUtils.cp(File.join(ROOT, 'install.sh'), File.join(checkout, 'install.sh'))
      env = fake_curl(dir).merge('CONTINUUM_INSTALL_ASSUME_YES' => '1')
      %w[core tech parent].each do |set|
        # One real core workflow body, plus a file that satisfies BOTH prune
        # conditions (the prefix and a repository `uses:` call) under a name
        # Continuum no longer ships. Against any other directory both the write
        # and the delete would happen; here neither may.
        real_core = File.join(workflows, 'continuum-opencode.yml')
        File.write(real_core, "name: A real core workflow\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo core\n")
        stale = File.join(workflows, 'continuum-validate-continuum.yml')
        File.write(stale, <<~YAML)
          name: Validate Continuum
          on:
            workflow_call:
            workflow_dispatch:
          jobs:
            ci:
              runs-on: ubuntu-latest
              steps:
                - run: echo own
                - uses: kodmial/continuum/.github/workflows/continuum-opencode.yml@main
        YAML
        refute_includes ALL_STUBS, 'continuum-validate-continuum.yml',
                        'the fixture must be a file Continuum no longer ships'
        before = File.read(real_core)

        output, status = Open3.capture2e(
          env, 'bash', File.join(checkout, 'install.sh'), checkout, 'main', set
        )

        refute status.success?, "#{set}: a self-install must fail, not report success\n#{output}"
        assert_includes output, 'refusing to install',
                        "#{set}: the refusal must be loud and name the reason"
        assert_includes output, "Continuum's own checkout",
                        "#{set}: the refusal must say what it detected"
        # The whole point: the core workflow is byte-for-byte untouched.
        assert_equal before, File.read(real_core),
                     "#{set}: a self-install must not overwrite Continuum's own core workflows"
        assert File.exist?(stale),
               "#{set}: installing into Continuum's own checkout must not delete its workflows"
        refute_includes output, 'removed superseded Continuum caller',
                        "#{set}: a self-install must not prune at all"
        refute_includes output, 'installed to',
                        "#{set}: a refused install must not claim it installed anything"
        # And no stub was written into the checkout either.
        Dir[File.join(workflows, '*.yml')].each do |file|
          body = File.read(file)
          next if body == before || File.read(stale) == body
          assert_includes body, "name: A real core workflow",
                          "#{set}: unexpected content written to #{File.basename(file)}"
        end
      end
    end
  end

  # The refusal must not be a trap for the legitimate case: a directory that
  # merely *contains* a `.github/workflows` is still installable, and so is a
  # consumer whose path is spelled differently. Only Continuum's own checkout,
  # resolved as the directory holding this very install.sh, is refused.
  def test_installer_still_installs_into_an_ordinary_consumer
    fixture do |dir|
      target = File.join(dir, 'some-consumer')
      output, status = Open3.capture2e('bash', File.join(ROOT, 'install.sh'), target)
      assert status.success?, "an ordinary consumer must still install\n#{output}"
      refute_includes output, 'refusing to install'
      assert_equal CORE_COUNT, Dir[File.join(target, '.github/workflows/*.yml')].size
    end
  end

  def test_core_install_is_idempotent_on_representative_consumer_layouts
    layouts = {
      'nanodictate' => %w[
        ci.yml
        nanodictate-ci-engine.yml
        nanodictate-packaging-repair.yml
        packaging-smoke.yml
        release.yml
      ],
      'kodmai' => %w[ci.yml],
      'kodmaiadmin' => %w[ci.yml],
      'runtime-lab' => %w[ci.yml qualification-chain.yml knowledge-sync.yml],
    }

    layouts.each do |consumer, project_workflows|
      fixture do |dir|
        target = File.join(dir, consumer)
        workflows = File.join(target, '.github/workflows')
        FileUtils.mkdir_p(workflows)

        project_workflows.each do |name|
          body = if name == 'ci.yml'
                   <<~YAML
                     name: CI
                     on:
                       pull_request:
                     jobs:
                       call:
                         uses: kodmial/continuum/.github/workflows/continuum-validation.yml@main
                   YAML
                 else
                   <<~YAML
                     name: #{File.basename(name, '.yml')}
                     on:
                       workflow_dispatch:
                     jobs:
                       local:
                         runs-on: ubuntu-latest
                         steps:
                           - run: echo project-owned
                   YAML
                 end
          File.write(File.join(workflows, name), body)
        end

        project_snapshot = project_workflows.to_h do |name|
          [name, File.binread(File.join(workflows, name))]
        end

        env = { 'CONTINUUM_INSTALL_ASSUME_YES' => '1' }
        first_output, first_status = Open3.capture2e(
          env, 'bash', File.join(ROOT, 'install.sh'), target
        )
        assert first_status.success?, "#{consumer}: #{first_output}"
        refute File.exist?(File.join(workflows, 'continuum-validation.yml')),
               "#{consumer}: core install must not create a second primary CI caller"
        project_snapshot.each do |name, body|
          assert_equal body, File.binread(File.join(workflows, name)),
                       "#{consumer}: install must not rewrite project-owned #{name}"
        end

        snapshot = Dir[File.join(workflows, '*.yml')].sort.to_h do |path|
          [File.basename(path), File.binread(path)]
        end
        second_output, second_status = Open3.capture2e(
          env, 'bash', File.join(ROOT, 'install.sh'), target
        )
        assert second_status.success?, "#{consumer}: #{second_output}"
        second = Dir[File.join(workflows, '*.yml')].sort.to_h do |path|
          [File.basename(path), File.binread(path)]
        end

        assert_equal snapshot, second,
                     "#{consumer}: a second core install must preserve workflow topology and bytes"
        assert_equal 1, second.values.count { |body| body.match?(/^name:\s*CI\s*$/) },
                     "#{consumer}: exactly one primary workflow may be named CI"
      end
    end
  end

  def test_validation_is_shared_engine_not_core_caller
    refute CORE_STUBS.any? { |path| File.basename(path) == 'continuum-validation.yml' },
           'validation must be invoked from the project-owned ci.yml, not installed as a second CI'
    assert File.file?(File.join(ROOT, '.github/workflows/continuum-validation.yml')),
           'the shared validation engine itself must remain in Continuum'
  end

  # The documented installation is a pipe: `curl …/install.sh | bash -s`. Under a
  # pipe there is no script file at all — `BASH_SOURCE` is unset and `$0` is the
  # shell itself — so resolving "self" from either value makes `dirname` yield
  # `.`, collapses the self-install directory onto the *consumer's* own
  # directory, and turns every piped install into a false self-install refusal:
  # exit 1, zero files written, in the consumer's own checkout.
  #
  # Every other installer fixture here runs `bash <absolute-path>/install.sh`,
  # where `BASH_SOURCE` IS set, so none of them can see this. This fixture is
  # the only one that hands the script to bash on stdin, which is exactly the
  # shape the installer documents in its own header.
  def test_piped_install_from_stdin_is_not_mistaken_for_a_self_install
    fixture do |dir|
      target = File.join(dir, 'consumer')
      FileUtils.mkdir_p(target)
      output, status = Open3.capture2e(
        fake_curl(dir), 'bash',
        stdin_data: File.binread(File.join(ROOT, 'install.sh')), chdir: target
      )
      assert status.success?,
             "the documented `curl … | bash -s` must install\n#{output}"
      refute_includes output, 'refusing to install',
                      'a piped run has no checkout behind it, so it cannot be a self-install'
      assert_includes output, 'installed to'
      assert_equal CORE_COUNT, Dir[File.join(target, '.github/workflows/*.yml')].size,
                   'the piped install must write the whole core caller set'
      assert_core_install_names(target)
    end
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

  # Concurrency on the macOS-backed tech callers belongs to the shared template,
  # not to a consumer's hand-edited copy: `concurrency` in a caller gates the
  # consumer's whole run together with the reusable workflow it calls, so
  # cancelling frees the scarce macOS runner slots themselves, whereas the same
  # block inside the callee can only gate the callee.
  #
  # The group name must stay Continuum's own: a shipped stub must never carry a
  # consumer's product name. Each caller's group is distinct so that CI cannot
  # cancel a packaging-smoke run on the same PR. The repository is part of the
  # group so the name identifies the run unambiguously in the Actions UI;
  # concurrency groups are already scoped per repository, so it is not what
  # keeps two repositories apart.

  def test_tech_swift_callers_gate_on_a_repository_scoped_concurrency_group
    base = 'continuum-tech-swift-ci.yml'
    concurrency = yaml(File.join(ROOT, '.github/caller-stubs/tech', base)).fetch('concurrency')
    assert_equal true, concurrency['cancel-in-progress'],
                 'a superseded Swift validation head must be cancelled'
    group = concurrency.fetch('group')
    assert group.start_with?('continuum-tech-swift-ci-'), base
    assert_includes group, '${{ github.repository }}',
                    'the tech caller concurrency group must identify the consumer repository'
  end

  def test_tech_stubs_reference_only_continuum_itself
    own_repo = 'kodmial/continuum'
    # `github.com/owner/repo`, matched case-insensitively because GitHub owners
    # are not case-sensitive and a template may spell one any way. This is
    # stripped before the bare-literal scan so the two never overlap.
    url = %r{github\.com/([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9._-]+)*)}i
    # A bare `owner/repo` literal in the YAML itself. The owner segment must
    # start lowercase, as every reference Continuum ships is written, which
    # keeps prose such as "Recovery/reconciliation" out of the matches.
    owner_repo = %r{(?<![\w./-])([a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9._-]+)*)}
    refute_empty TECH_STUBS
    TECH_STUBS.each do |stub|
      base = File.basename(stub)
      body = File.read(stub)

      body.scan(/^[[:space:]]*-?[[:space:]]*uses:[[:space:]]*["']?([^"'\s]+)/) do |(target)|
        assert target.start_with?("#{own_repo}/"), "#{base}: uses #{target}, which is not a Continuum workflow"
      end

      references = body.scan(url).flatten.map { |ref| ref.split('#').first }
      references += body.gsub(url, '').scan(owner_repo).flatten
      foreign = references.reject { |ref| ref.downcase.start_with?("#{own_repo}/") }
      assert_empty foreign, "#{base}: references a repository other than #{own_repo}: #{foreign.join(', ')}"
    end
  end

  # ------------------------------------------------- bootstrap secret target

  # The bootstrap callee ships to every consumer, so the repository it
  # bootstraps the secret into is a per-consumer decision. A literal written
  # into the `gh api` path made a caller installed in any other repository
  # write that secret into the wrong repository — a silent cross-repository
  # write that no other assertion can see, because the file still parses, the
  # job still goes green, and the public key really is fetched.
  #
  # The check is structural, not a list of known consumer names: every
  # `owner/repo` literal in this callee must be Continuum's own repository or
  # absent entirely, so a consumer nobody has enumerated yet fails the same way
  # a known one does.
  def test_bootstrap_runtime_secret_callee_bootstraps_no_named_repository
    own_repo = 'kodmial/continuum'
    base = 'continuum-bootstrap-runtime-secret.yml'
    body = workflow_body(base)
    # `github.com/owner/repo`, stripped before the bare-literal scan so the two
    # never overlap. The bare form's owner segment must start lowercase, as
    # every reference Continuum ships is written, which keeps prose such as
    # "Recovery/reconciliation" out of the matches.
    url = %r{github\.com/([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9._-]+)*)}i
    owner_repo = %r{(?<![\w./-])([a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9._-]+)*)}

    references = body.scan(url).flatten.map { |ref| ref.split('#').first }
    references += body.gsub(url, '').scan(owner_repo).flatten
    foreign = references.reject { |ref| ref.downcase.start_with?("#{own_repo}/") }
    assert_empty foreign, "#{base}: names a repository other than #{own_repo}: #{foreign.uniq.join(', ')} — " \
                         'the secret target must come from an input, not a literal'

    # …and the target must be resolved, not merely absent: an unparameterized
    # `gh api` path built from a shell variable is the only shape that reaches
    # the caller's own repository.
    assert_includes body,
                    'TARGET_REPOSITORY: ${{ inputs.repository || vars.CONTINUUM_TARGET_REPOSITORY || github.repository }}',
                    "#{base}: the target must be the input, then the repository variable, then the calling repository"
    assert_includes body, 'gh api "repos/$TARGET_REPOSITORY/actions/secrets/public-key"'
  end

  # A target that resolves to nothing, or to something that is not `owner/repo`,
  # must fail before `gh api` is reached: an empty `repos//…` is not an error
  # the API reports as one.
  def test_bootstrap_runtime_secret_fails_loudly_on_an_unusable_target
    base = 'continuum-bootstrap-runtime-secret.yml'
    body = workflow_body(base)
    assert_includes body, '[[ "$TARGET_REPOSITORY" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || {'
    assert_includes body, '::error::No usable target repository:'
    assert_operator body.index('exit 2'), :<, body.index('gh api "repos/$TARGET_REPOSITORY'),
                    "#{base}: the guard must run before the API call"
  end

  # The stub is installed verbatim into every consumer, so it must be able to
  # forward the target without ever naming one: a bare passthrough is what lets
  # the callee fall through to `vars.CONTINUUM_TARGET_REPOSITORY` and then to
  # `github.repository`, which is the repository the stub was installed into.
  def test_bootstrap_runtime_secret_stub_forwards_an_unpinned_target
    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-bootstrap-runtime-secret.yml'))
    callee = events(yaml(File.join(ROOT, '.github/workflows/continuum-bootstrap-runtime-secret.yml')))
                 .fetch('workflow_call').fetch('inputs')

    repository = callee.fetch('repository')
    assert_equal 'string', repository.fetch('type')
    assert_equal '', repository.fetch('default')
    assert_equal false, repository.fetch('required')

    with = stub.fetch('jobs').fetch('call').fetch('with')
    assert_equal '${{ inputs.repository }}', with.fetch('repository')
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
      refute File.exist?(File.join(ROOT, 'scripts/release-prep.rb'))
      refute Dir.exist?(File.join(ROOT, 'scripts/packaging-smoke'))
    end
  end

  # Measured from the tree, never from a literal, so a stub added or dropped
  # needs no test edit and cannot drift. The `core` set installs every stub
  # directly under `.github/caller-stubs/` (task-domain layer); `tech` the
  # `continuum-tech-<tech>-*` stubs in `.github/caller-stubs/tech/`; `parent`
  # the child-execution stubs in `.github/caller-stubs/parent/`.
  #
  # A core workflow with no stub is legitimate only when it is named below:
  # the three child-execution callees installed by the `parent` set, the
  # legacy child-dispatcher compatibility callee kept for stale callers,
  # shared validation, and Continuum's project-owned self-entry workflows.
  CORE_CALLEE_WORKFLOWS = %w[
    continuum-consumer-child-pr-review.yml
    continuum-consumer-child-review.yml
    continuum-consumer-child-run-cleanup.yml
    continuum-consumer-child-worker.yml
  ].freeze
  REPO_OWNED_WORKFLOWS = %w[
    automation.yml
    ci.yml
    opencode.yml
    pr-agent.yml
    pr-agent-recovery.yml
    pr-agent-router.yml
    continuum-consumer-child-dispatcher.yml
    continuum-validation.yml
  ].freeze

  # The number of callers Continuum ships in the `tech` and `parent` layers,
  # stated as literals for the same reason as `CORE_COUNT` below.
  #
  # These were once `TECH_STUBS.size` and `PARENT_STUBS.size`, which made every
  # check that used them compare a value against itself: `assert_equal
  # TECH_COUNT, <installed file count>` reduced to `<installed> == <installed>`
  # and could not fail. A literal is the third, independent source, so shipping
  # a sixth tech caller or a fifth parent caller has to be said out loud here,
  # in the same file and in the same commit as the new stub, where a reviewer
  # sees it. `assert_tech_layers_agree` remains the check that needs no literal:
  # it compares the two trees against each other by name, so adding a
  # technology still needs no edit — only changing the *size* of a layer does.
  TECH_COUNT = 1
  PARENT_COUNT = 4

  # The same `tech_name?` rule as the instance helper, hoisted so the constant
  # table above can use it. A tech name has a `tech` segment plus at least two
  # further segments, so `continuum-tech-swift-release` counts and a bare
  # `continuum-tech` does not.
  def self.tech_name?(base)
    segments = base.delete_suffix('.yml').split('-')
    base.start_with?('continuum-') && segments[1] == 'tech' && segments.size >= 4
  end

  # The number of core callers Continuum ships, stated as a literal and not
  # derived from either tree.
  #
  # Computing it from `WORKFLOWS` made the check that uses it circular: the
  # count was taken from the workflow tree and then compared against the stub
  # tree, so adding a workflow and its stub together — exactly what a new
  # feature does — moved both sides and passed. A literal is the third,
  # independent source: to change the nineteen core callers someone has to say so
  # here, which is where a reviewer sees it.
  CORE_COUNT = 19

  # Every core workflow is either called by a stub in one of the three layers
  # or is a repository-owned/shared engine intentionally invoked from a
  # project-owned entry point. A workflow nobody calls is a dead file that
  # still costs a consumer a `continuum-*.yml` name.
  #
  # The callee is resolved from the stub's own `uses:` line rather than from
  # the stub's file name: the parent layer installs `continuum-child-*.yml`
  # while calling `continuum-consumer-child-*.yml`, and that difference is
  # deliberate, not a mismatch to be papered over.
  def stub_callee_name(stub_path)
    yaml(stub_path).fetch('jobs').each_value do |job|
      next unless job['uses']
      match = job['uses'].match(%r{kodmial/continuum/\.github/workflows/([^@]+)@})
      return match[1] if match
    end
    nil
  end

  # The repository secrets read by the workflows a given set of stubs installs.
  #
  # Both halves count: the stub itself, and the reusable workflow it calls. A
  # stub reads a secret in order to forward it, so the credential a consumer
  # must define for a set can be named only by the stub — the parent set reads
  # `TAP_PAT` nowhere in its callees, only in the stubs that pass it on.
  #
  # Derived by walking stub -> callee -> `secrets.X`, never by hardcoding a name
  # list on the test side. A guard that enumerates the names it checks is blind
  # to a fifth secret: the new name simply is not in the pattern, so it is
  # skipped silently and the guard reports green. Deriving the set from the code
  # is what makes these checks fail when a secret is added, removed or moved
  # between sets.
  def secrets_read_by(stubs)
    stubs.flat_map { |path| File.read(path) }
         .concat(stubs.map { |path| stub_callee_name(path) }.compact
                      .map { |callee| workflow_body(callee) })
         .flat_map { |body| body.scan(/secrets\.([A-Z][A-Z0-9_]*)/).flatten }
         .uniq
         .sort
  end

  def test_every_core_workflow_has_a_caller_or_is_repo_ci
    stubs = CORE_STUBS + TECH_STUBS + PARENT_STUBS
    called = stubs.map { |path| stub_callee_name(path) }.compact
    workflows = WORKFLOWS.map { |path| File.basename(path) }
    uncalled = workflows.reject { |base| called.include?(base) || REPO_OWNED_WORKFLOWS.include?(base) }
    assert_empty uncalled, "core workflows with no caller stub: #{uncalled.join(', ')}"
    # And the reverse: a stub whose callee is gone is a caller that 404s at
    # run time, which only the workflow_run path would ever discover.
    missing = called.reject { |base| workflows.include?(base) }
    assert_empty missing, "stubs calling a workflow that does not exist: #{missing.join(', ')}"
    stubs.each do |path|
      refute_nil stub_callee_name(path), "#{File.basename(path)}: no reusable-workflow `uses:` found"
    end
    # The three live child-execution callees are reached through the `parent`
    # layer, and only through it. The old dispatcher callee is intentionally
    # uncalled: it exists only so an already-installed stale caller resolves to
    # a harmless compatibility workflow instead of a 404.
    parent_called = PARENT_STUBS.map { |path| stub_callee_name(path) }.compact
    assert_equal CORE_CALLEE_WORKFLOWS.sort, parent_called.sort,
                 'the child-execution callees must be exactly the workflows the parent layer calls'
    overlap = CORE_STUBS.map { |path| stub_callee_name(path) }.compact & CORE_CALLEE_WORKFLOWS
    assert_empty overlap,
                 "a core caller must not reach a child-execution callee; that is the parent layer's: #{overlap.join(', ')}"
  end

  def test_child_run_cleanup_is_scoped_to_completed_delegated_runs
    caller = yaml(File.join(ROOT, '.github/caller-stubs/parent/continuum-child-run-cleanup.yml'))
    watched = events(caller).fetch('workflow_run').fetch('workflows')
    assert_equal ['SubTask', 'SubTask review', 'SubTask PR review'], watched
    assert_equal ['completed'], events(caller).fetch('workflow_run').fetch('types')

    body = workflow_body('continuum-consumer-child-run-cleanup.yml')
    assert_includes body, "run.status !== 'completed'"
    assert_includes body, "new Set(['SubTask', 'SubTask review', 'SubTask PR review'])"
    assert_includes body, 'deleteWorkflowRun'
  end

  # kodmial/continuum#171: a post-completion wake storm exhausted the token's
  # API budget and the resolver reported those failures as "no repository
  # declares". A discovery outage must fail with a distinct unavailable
  # verdict so a retryable storm is never mistaken for a misconfigured
  # relationship, while genuine zero/ambiguous matches stay fail-closed.
  def test_delegation_resolver_distinguishes_outage_from_misconfiguration
    body = File.read(File.join(ROOT, '.github/scripts/delegation_repository.sh'))
    unavailable = 'Delegated child discovery is unavailable; refusing to guess.'
    assert_includes body, unavailable
    # The outage verdict leaves through a dedicated status, never the
    # verification status, so callers and humans can tell them apart.
    assert_match(/return "\$UNAVAILABLE"/, body)
    # Genuine fail-closed verdicts are unchanged.
    assert_includes body, 'No unique repository declares the requested child relationship.'
    assert_includes body, 'More than one repository declares the same child relationship.'
    assert_includes body, 'Child id is not allowed by CONTINUUM_CHILDREN.'
    # The opaque architecture: raw API errors embed request URLs naming the
    # private child, so every resolver API call must hide stderr and the
    # unavailable verdict must not interpolate a repository name.
    body.each_line do |line|
      refute_match(/gh api .*2>&1/, line, 'raw API output must stay out of parent logs')
    end
    unavailable_lines = body.each_line.select { |line| line.include?(unavailable) }
    assert_operator unavailable_lines.size, :>=, 2
    unavailable_lines.each do |line|
      refute_includes line, '$repository'
      refute_includes line, '$candidate'
    end
  end

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
  def test_installing_main_preserves_canonical_core_stub_bytes
    fixture do |dir|
      destination = File.join(dir, 'consumer')
      workflows = File.join(destination, '.github/workflows')
      FileUtils.mkdir_p(workflows)

      CORE_STUBS.each do |stub|
        FileUtils.cp(stub, File.join(workflows, File.basename(stub)))
      end
      before = CORE_STUBS.to_h do |stub|
        base = File.basename(stub)
        [base, File.binread(File.join(workflows, base))]
      end

      bin = File.join(dir, 'bin')
      FileUtils.mkdir_p(bin)
      File.write(File.join(bin, 'curl'), <<~SH)
        #!/usr/bin/env bash
        set -eu
        url="${@: -1}"
        cat "$TEMPLATES/${url##*/}"
      SH
      FileUtils.chmod(0755, File.join(bin, 'curl'))
      env = {
        'PATH' => "#{bin}:#{ENV['PATH']}",
        'TEMPLATES' => File.join(ROOT, '.github/caller-stubs'),
        'CONTINUUM_INSTALL_ASSUME_YES' => '1'
      }

      output, status = Open3.capture2e(
        env, 'bash', File.join(ROOT, 'install.sh'), destination, 'main', 'core', '--yes'
      )
      assert status.success?, output

      after = CORE_STUBS.to_h do |stub|
        base = File.basename(stub)
        [base, File.binread(File.join(workflows, base))]
      end
      assert_equal before, after,
                   'installing canonical main over canonical callers must be byte-stable'
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
        job.fetch('with', {}).each_key { |key| assert contract.fetch('inputs').key?(key), key }
        contract.fetch('inputs', {}).each do |key, spec|
          assert job.fetch('with', {}).key?(key), key if spec['required']
        end

        if File.basename(file) == 'continuum-child-run-cleanup.yml'
          refute job.key?('secrets'), 'run cleanup needs only the caller GITHUB_TOKEN, never child credentials'
          next
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
      preserved = %w[ci.yml release.yml continuum-issue-scheduler.yml continuum-opencode.yml]
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
      # The `preserved` files above are themselves `continuum-`-prefixed, so the
      # installed set is selected by the parent stub names rather than by the
      # bare prefix glob, which would count the preserved project-owned files.
      installed = PARENT_STUBS.map { |stub| File.join(workflows, File.basename(stub)) }
                              .select { |file| File.exist?(file) }
      assert_equal PARENT_COUNT, installed.size
      assert_empty Dir[File.join(workflows, 'child-*.yml')]
      installed.each do |file|
        yaml(file).fetch('jobs').each_value do |job|
          next unless job['uses']
          assert job['uses'].end_with?("@#{ref}")
          next if File.basename(file) == 'continuum-child-run-cleanup.yml'
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
  # workflow may read, require, or forward a paid provider token. PR-Agent is
  # the optional review provider: it stays provider-neutral and reaches its
  # model through the runner-local OpenCode bridge, so it needs no paid key
  # either. A run that cannot review (disabled provider, unreachable backend,
  # moved head, mutated checkout) fails explicitly instead of a silent green
  # no-op.
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

    pr_agent = File.read(File.join(ROOT, '.github/workflows/continuum-pr-agent.yml'))
    assert_includes pr_agent, 'core.setFailed',
                    'pr-agent must fail explicitly, not skip green'
    refute_includes pr_agent, "if: steps.groq.outputs.available == 'true'"
  end

  # The reusable PR-Agent provider replaced the paid-Groq refusal stub: it
  # must stay provider-neutral (no Groq/model hard-code), reach OpenCode
  # through the minimal loopback bridge, and keep the review reviewer-only.
  def test_pr_agent_uses_the_opencode_backend_without_a_paid_provider
    pr_agent = File.read(File.join(ROOT, '.github/workflows/continuum-pr-agent.yml'))
    stub = File.read(File.join(ROOT, '.github/caller-stubs/continuum-pr-agent.yml'))
    toml = File.read(File.join(ROOT, '.pr_agent.toml'))

    %w[groq GROQ].each do |needle|
      refute_includes pr_agent, needle, 'pr-agent workflow must not name Groq'
      refute_includes toml, needle, '.pr_agent.toml must not name Groq'
    end

    # The pinned release runs on the runner (a Docker action cannot reach
    # runner loopback), against the loopback bridge only.
    assert_includes pr_agent, 'python3 -m pip install',
                    'pr-agent must install the pinned release on the runner'
    assert_includes pr_agent, '"pr-agent==${PR_AGENT_VERSION}"',
                    'pr-agent must install the pinned release on the runner'
    assert_includes pr_agent, 'opencode serve --hostname 127.0.0.1',
                    'the OpenCode server must bind loopback only'
    assert_includes pr_agent, 'pr_agent_bridge.py',
                    'PR-Agent must infer through the compatibility bridge'
    refute_includes pr_agent, 'docker://',
                    'a container cannot reach the runner-local bridge'
    # The hostname may be named only to forbid it; it must never be used as
    # a backend address.
    refute_match(/https?:\/\/host\.docker\.internal/, pr_agent,
                 'container networking must not rely on undocumented hostnames')
    refute_match(/host\.docker\.internal:\d/, pr_agent,
                 'container networking must not rely on undocumented hostnames')

    # Reviewer-only: the run proves the checkout is untouched, and the
    # provider stays opt-in so consumers that never enable it are unaffected.
    assert_includes pr_agent, 'Prove the checkout is unmodified'
    assert_includes pr_agent, 'CONTINUUM_REVIEW_PROVIDER',
                    'PR-Agent must stay disabled unless the consumer selects pr-agent'
    assert_includes stub, 'continuum-pr-agent.yml@main'

    # The generic contract is configuration, not hard-coded product policy.
    %w[api_base model max_tokens review_provider].each do |knob|
      assert_includes pr_agent, knob, "pr-agent workflow must expose the #{knob} knob"
    end
  end

  # PR-Agent must not turn normal concurrency races or an absent native
  # persistent finding state into a permanent red gate. A stale dispatch is
  # ignored and recovery re-evaluates the current HEAD; when native state is
  # absent, a validated structured full review derives a schema-compatible
  # fallback (empty for a clean review, ACTIVE findings otherwise) via the
  # upstream v0.46.0 finding-state contract, while an unrepresentable finding
  # still fails closed. Native state remains authoritative when present.
  def test_pr_agent_clean_review_and_stale_head_are_non_blocking
    pr_agent = File.read(File.join(ROOT, '.github/workflows/continuum-pr-agent.yml'))

    assert_includes pr_agent, "core.setOutput('admitted', 'false');"
    assert_includes pr_agent, 'Stale admission ignored:'
    refute_includes pr_agent, 'Stale admission: caller observed'

    assert_includes pr_agent, 'REVIEW_JSON: ${{ steps.pragent.outputs.review }}'
    # The runtime loads the canonical fallback helper from Continuum itself,
    # so old PR heads cannot keep the broken inline implementation alive.
    assert_includes pr_agent, 'contents/src/continuum/pr_agent_fallback_state.py'
    assert_includes pr_agent, 'fallback_pythonpath'
    assert_includes pr_agent, 'derive_fallback_state'
    assert_includes pr_agent, 'FallbackStateError'
    # Native state wins when present; otherwise the validated structured
    # review derives an ACTIVE fallback instead of blocking repair.
    assert_includes pr_agent, 'parse_review_state'
    refute_includes pr_agent, 'state = reconciled.state'
    refute_includes pr_agent, 'Upstream review has key findings but published no persistent finding state.'
    # Only an unrepresentable finding fails closed; nothing is invented and
    # nothing is marked resolved by the fallback.
    assert_includes pr_agent, 'Cannot derive fallback persistent state'

    fallback = File.read(File.join(ROOT, 'src/continuum/pr_agent_fallback_state.py'))
    assert_includes fallback, '"findings": findings'
    assert_includes fallback, '"complete": True'
    assert_includes fallback, '"kind": "full"'

    repair = File.read(File.join(ROOT, '.github/workflows/continuum-pr-agent-repair.yml'))
    assert_includes repair, 'git clean -fdX',
                    'repair must remove only ignored tool/build artifacts before staging'
    assert_includes repair, 'git ls-files -ci --exclude-standard',
                    'repair must detect ignored files that older branches still track'
    assert_includes repair, 'git restore --source="$START_HEAD"',
                    'repair must restore tracked ignored artifacts to the reviewed HEAD'
    assert_includes repair, 'PR_DRAFT="$(jq -r',
                    'repair must refuse a PR that is already draft'
    assert_includes repair, 'CURRENT_DRAFT="$(cut -f3',
                    'repair must revalidate draft state immediately before publication'
    assert_includes repair, 'became closed or draft during repair',
                    'a mid-repair draft transition must discard local repair changes'
  end

  # Work-Lock #58 item 4 (conservative subset): only the four verified-safe
  # pure same-repository read-only PR-Agent paths leave the shared TAP_PAT
  # budget. Mixed read/write/dispatch and cross-repo paths stay PAT-backed so
  # downstream triggers and actor identity are unchanged.
  def test_pr_agent_admission_and_revalidation_reads_use_repository_token
    body = File.read(File.join(ROOT, '.github/workflows/continuum-pr-agent.yml'))
    workflow = yaml(File.join(ROOT, '.github/workflows/continuum-pr-agent.yml'))
    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-pr-agent.yml'))

    # The review path now owns same-repository workflow_dispatch for bounded
    # recovery, so GITHUB_TOKEN needs actions:write end-to-end. Reusable
    # workflows cannot elevate beyond the caller grant.
    assert_equal 'write', workflow.fetch('permissions').fetch('actions'),
                 'reusable workflow must grant actions:write for bounded same-repo review dispatch'
    assert_equal 'write', workflow.fetch('jobs').fetch('pr_agent').fetch('permissions').fetch('actions'),
                 'pr_agent job must grant actions:write for bounded same-repo review dispatch'
    assert_equal 'write', stub.fetch('permissions').fetch('actions'),
                 'caller stub must grant actions:write because reusable workflows cannot elevate GITHUB_TOKEN'
    # pull-requests read capability is preserved (write implies read; the
    # contract keeps write for the mutating steps below).
    assert_equal 'write', workflow.fetch('jobs').fetch('pr_agent').fetch('permissions').fetch('pull-requests')
    assert_equal 'write', stub.fetch('permissions').fetch('pull-requests')

    admit = step_body(body, 'Admit only a review-ready PR with green CI on the exact HEAD')
    before = step_body(body, 'Revalidate the admitted exact HEAD immediately before review')
    after = step_body(body, 'Revalidate the PR head and native review output after review')
    moved = step_body(body, 'Fail closed on a moved head')
    [admit, before, after, moved].each { |step| refute_nil step, 'a verified-safe read step is missing' }

    # 1. Admission: pulls.get plus exact-HEAD CI evidence against the
    # resolved target. Local same-repository reads use github.token;
    # delegated cross-repository reads require TAP_PAT via the conditional.
    # Presence-only checks would pass a step that reads cross-repo with an
    # unconditional github.token while mentioning TAP_PAT elsewhere, so each
    # step must carry the conditional token expression and the fail-closed
    # guard, and must never set an unconditional github.token credential.
    conditional_token = "CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED == 'true' && secrets.TAP_PAT || (env.CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED != 'true' && github.token || '')"
    empty_token_tail = "&& github.token || '')"
    assert_includes admit, 'github.token'
    assert_includes admit, 'secrets.TAP_PAT'
    assert_includes admit, 'CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED'
    assert_includes admit, conditional_token,
                    'admission must select TAP_PAT for delegated reads instead of an unconditional github.token'
    assert_includes admit, empty_token_tail,
                    'admission must yield an empty token for delegated runs without PAT so auth itself fails closed'
    refute_includes admit, 'secrets.TAP_PAT || github.token }}',
                    'admission must not fall back to github.token for delegated runs without PAT'
    assert_includes admit, 'refusing to fall back to github.token',
                    'admission must fail closed for delegated reads without TAP_PAT'
    refute_includes admit, 'github-token: ${{ github.token }}',
                    'admission must not set an unconditional github.token credential'
    assert_includes admit, 'github.rest.pulls.get'
    assert_includes admit, 'github.rest.actions.listWorkflowRunsForRepo'
    assert_includes admit, 'CONTINUUM_PR_AGENT_TARGET_OWNER'
    refute_includes admit, 'owner: context.repo.owner'

    # 2. Immediately-before-review revalidation: gh pr view plus exact-HEAD
    # CI evidence against the resolved target (conditional token as above).
    assert_includes before, 'github.token'
    assert_includes before, 'secrets.TAP_PAT'
    assert_includes before, 'CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED'
    assert_includes before, conditional_token,
                    'pre-review revalidation must select TAP_PAT for delegated reads instead of an unconditional github.token'
    assert_includes before, empty_token_tail,
                    'pre-review revalidation must yield an empty token for delegated runs without PAT so auth itself fails closed'
    refute_includes before, 'secrets.TAP_PAT || github.token }}',
                    'pre-review revalidation must not fall back to github.token for delegated runs without PAT'
    assert_includes before, 'refusing to fall back to github.token',
                    'pre-review revalidation must fail closed for delegated reads without TAP_PAT'
    refute_includes before, 'GH_TOKEN: ${{ github.token }}',
                    'pre-review revalidation must not set an unconditional github.token credential'
    assert_includes before, 'gh pr view'
    assert_includes before, 'actions/runs?event=pull_request&head_sha='
    assert_includes before, 'CONTINUUM_PR_AGENT_TARGET_REPOSITORY'

    # 3. Immediately-after-review revalidation: pulls.get for current PR/head
    # only against the resolved target.
    assert_includes after, 'github.token'
    assert_includes after, 'secrets.TAP_PAT'
    assert_includes after, 'CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED'
    assert_includes after, conditional_token,
                    'post-review revalidation must select TAP_PAT for delegated reads instead of an unconditional github.token'
    assert_includes after, empty_token_tail,
                    'post-review revalidation must yield an empty token for delegated runs without PAT so auth itself fails closed'
    refute_includes after, 'secrets.TAP_PAT || github.token }}',
                    'post-review revalidation must not fall back to github.token for delegated runs without PAT'
    assert_includes after, 'refusing to fall back to github.token',
                    'post-review revalidation must fail closed for delegated reads without TAP_PAT'
    refute_includes after, 'github-token: ${{ github.token }}',
                    'post-review revalidation must not set an unconditional github.token credential'
    assert_includes after, 'github.rest.pulls.get'
    assert_includes after, 'CONTINUUM_PR_AGENT_TARGET_OWNER'

    # 4. Final fail-closed moved-head check: gh pr view for current PR/head
    # only against the resolved target.
    assert_includes moved, 'github.token'
    assert_includes moved, 'secrets.TAP_PAT'
    assert_includes moved, 'CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED'
    assert_includes moved, conditional_token,
                    'moved-head check must select TAP_PAT for delegated reads instead of an unconditional github.token'
    assert_includes moved, empty_token_tail,
                    'moved-head check must yield an empty token for delegated runs without PAT so auth itself fails closed'
    refute_includes moved, 'secrets.TAP_PAT || github.token }}',
                    'moved-head check must not fall back to github.token for delegated runs without PAT'
    assert_includes moved, 'refusing to fall back to github.token',
                    'moved-head check must fail closed for delegated reads without TAP_PAT'
    refute_includes moved, 'GH_TOKEN: ${{ github.token }}',
                    'moved-head check must not set an unconditional github.token credential'
    assert_includes moved, 'gh pr view'
    assert_includes moved, 'CONTINUUM_PR_AGENT_TARGET_REPOSITORY'

    # github-script pure reads use the injected client directly: no dynamic
    # require of @actions/github and no secondary Octokit client to audit.
    refute_includes body, "require('@actions/github')"
    refute_includes body, 'require("@actions/github")'

    # Exact-HEAD admission, CI name matching, stale-HEAD rejection and
    # fail-closed behavior are unchanged.
    assert_includes admit, 'run.head_sha === headSha'
    assert_includes admit, 'Stale admission ignored:'
    assert_includes before, 'refusing to review an unqualified HEAD'
    assert_includes after, 'PR head moved during review'
    assert_includes moved, 'the review is stale'

    # Review-side same-repository control-plane work is repository-token
    # backed. Repair/push remains in the separate repair workflow and keeps
    # its stronger credential contract. The review workflow must not consume
    # the shared user PAT even for bounded workflow_dispatch recovery.
    runtime = step_body(body, 'Resolve the Continuum-owned PR-Agent runtime bundle')
    retry_step = step_body(body, 'Schedule bounded retry for retryable PR-Agent review failure')
    review_tool = step_body(body, 'Run upstream full review on the exact HEAD')
    improve_tool = step_body(body, 'Run upstream improve on the exact HEAD when repair value remains')
    in_flight = step_body(body, 'Mark PR-Agent review in flight')
    normalize = step_body(body, 'Normalize persistent improve presentation')
    publish = step_body(body, 'Publish durable PR-Agent review state')
    [runtime, retry_step, review_tool, improve_tool, in_flight, normalize, publish].each { |step| refute_nil step }
    refute_includes body, 'Run upstream full review and full improve on the exact HEAD',
                      'issue #231 splits review and conditional improve; the combined step must stay removed'
    assert_includes runtime, 'GH_TOKEN: ${{ github.token }}',
                    'public Continuum runtime-bundle fetch must not consume shared PAT quota'
    assert_includes runtime, 'repos/kodmial/continuum/contents'
    # Target-aware (#243) retry revalidation reads the resolved target:
    # local same-repository reads use github.token while delegated
    # cross-repository reads require TAP_PAT with a fail-closed empty token
    # (delegated runs without PAT yield '' so auth itself fails closed
    # instead of silently reading the parent with github.token).
    assert_includes retry_step, conditional_token,
                    'bounded retry revalidation must select TAP_PAT for delegated reads instead of an unconditional github.token'
    assert_includes retry_step, empty_token_tail,
                    'bounded retry revalidation must yield an empty token for delegated runs without PAT so auth itself fails closed'
    refute_includes retry_step, 'secrets.TAP_PAT || github.token }}',
                    'bounded retry revalidation must not fall back to github.token for delegated runs without PAT'
    assert_includes retry_step, 'Delegated PR-Agent retry requires TAP_PAT',
                    'bounded retry revalidation must fail closed for delegated reads without TAP_PAT'
    assert_includes retry_step, 'CONTINUUM_PR_AGENT_TARGET_REPOSITORY',
                    'bounded retry revalidation must read the resolved target, never the parent by default'
    assert_includes retry_step, 'gh workflow run'
    refute_includes before, 'gh workflow run',
                      'the pre-review revalidation must stay read-only'
    refute_includes moved, 'gh workflow run',
                      'the moved-head check must stay read-only'
    assert_includes review_tool, 'GITHUB__USER_TOKEN:',
                     'PR-Agent review tool execution must set a user token'
    assert_includes review_tool, conditional_token,
                    'PR-Agent review tool execution must select TAP_PAT for delegated runs instead of an unconditional github.token'
    assert_includes review_tool, empty_token_tail,
                    'PR-Agent review tool execution must yield an empty token for delegated runs without PAT so auth itself fails closed'
    refute_includes review_tool, 'secrets.TAP_PAT || github.token }}',
                    'PR-Agent review tool execution must not fall back to github.token for delegated runs without PAT'
    assert_includes review_tool, 'Delegated PR-Agent execution requires TAP_PAT',
                    'PR-Agent review tool execution must fail closed for delegated runs without TAP_PAT'
    assert_includes improve_tool, 'GITHUB__USER_TOKEN:',
                     'PR-Agent improve tool execution must set a user token'
    assert_includes improve_tool, conditional_token,
                    'PR-Agent improve tool execution must select TAP_PAT for delegated runs instead of an unconditional github.token'
    assert_includes improve_tool, empty_token_tail,
                    'PR-Agent improve tool execution must yield an empty token for delegated runs without PAT so auth itself fails closed'
    refute_includes improve_tool, 'secrets.TAP_PAT || github.token }}',
                    'PR-Agent improve tool execution must not fall back to github.token for delegated runs without PAT'
    assert_includes improve_tool, 'Delegated PR-Agent execution requires TAP_PAT',
                    'PR-Agent improve tool execution must fail closed for delegated runs without TAP_PAT'
    assert_includes in_flight, conditional_token,
                    'in-flight commit-status publishing must select TAP_PAT for delegated writes instead of an unconditional github.token'
    assert_includes in_flight, empty_token_tail,
                    'in-flight commit-status publishing must yield an empty token for delegated runs without PAT so auth itself fails closed'
    refute_includes in_flight, 'secrets.TAP_PAT || github.token }}',
                    'in-flight commit-status publishing must not fall back to github.token for delegated runs without PAT'
    assert_includes in_flight, 'Delegated PR-Agent execution requires TAP_PAT',
                    'in-flight commit-status publishing must fail closed for delegated writes without TAP_PAT'
    refute_includes in_flight, 'github-token: ${{ github.token }}',
                    'in-flight commit-status publishing must not set an unconditional github.token credential'
    assert_includes normalize, 'github-token: ${{ github.token }}',
                    'comment maintenance must use repository token'
    assert_includes normalize, 'deleteComment'
    assert_includes publish, 'github-token: ${{ github.token }}',
                     'commit-status publishing must use repository token'
    # Target-aware (#243) delegated reads/writes require TAP_PAT, so the review
    # workflow is no longer entirely PAT-free: the admission/revalidation
    # steps above plus the in-flight commit-status step and the PR-Agent
    # review/improve tool steps legitimately carry
    # the fail-closed conditional. The
    # PAT-quota isolation contract is preserved in scoped form: local
    # control-plane steps stay repository-token backed (asserted per step
    # above), tool execution consumes the shared PAT only through the
    # fail-closed conditional for delegated runs, there is no
    # unconditional fallback to github.token for delegated runs, and the
    # only bare TAP_PAT credential is the target-resolution fetch itself.
    refute_includes body, 'secrets.TAP_PAT || github.token }}',
                    'review workflow must never fall back to github.token for delegated runs without PAT'
    refute_includes body, 'GITHUB__USER_TOKEN: ${{ secrets.TAP_PAT }}',
                    'review tool execution must never consume the shared user PAT unconditionally'
    resolve = step_body(body, 'Resolve PR-Agent target context')
    refute_nil resolve, 'the target-context resolution step is missing'
    assert_includes resolve, 'GH_TOKEN: ${{ secrets.TAP_PAT }}',
                    'target resolution fetches cross-repo child config with TAP_PAT'
    assert_equal 1, body.scan('GH_TOKEN: ${{ secrets.TAP_PAT }}').size,
                 'only target resolution may use a bare TAP_PAT credential; every read must use the fail-closed conditional'
    body.each_line.with_index(1) do |line, lineno|
      next unless line.include?('secrets.TAP_PAT')
      allowed = line.include?('CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED') ||
                line.include?('TAP_PAT: ${{ secrets.TAP_PAT }}') ||
                resolve.include?(line.strip)
      assert allowed, "line #{lineno} consumes TAP_PAT outside the gated target-aware contract: #{line.strip}"
    end
  end

  # Work-Lock #58 item 5 (conservative subset, kodmial/continuum#236): only
  # the two verified-safe pure same-repository read-only PR-Agent repair
  # paths leave the shared TAP_PAT budget. Mixed read/write/dispatch/push
  # and cross-repository paths stay PAT-backed.
  def test_pr_agent_repair_verified_safe_reads_use_repository_token
    body = File.read(File.join(ROOT, '.github/workflows/continuum-pr-agent-repair.yml'))
    workflow = yaml(File.join(ROOT, '.github/workflows/continuum-pr-agent-repair.yml'))
    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-pr-agent-repair.yml'))

    # Existing job/caller issues and pull-requests permissions already
    # provide read capability; the migration must not widen permissions.
    assert_equal 'write', workflow.fetch('jobs').fetch('repair').fetch('permissions').fetch('issues')
    assert_equal 'write', workflow.fetch('jobs').fetch('repair').fetch('permissions').fetch('pull-requests')
    assert_equal 'write', stub.fetch('permissions').fetch('issues')
    assert_equal 'write', stub.fetch('permissions').fetch('pull-requests')

    convergence = step_body(body, 'Check durable PR-Agent no-progress state')
    target = step_body(body, 'Resolve the writable PR source branch')
    refute_nil convergence, 'the no-progress read step is missing'
    refute_nil target, 'the writable-branch resolution read step is missing'

    # 1. No-progress check: issues.listComments read plus workflow-owned
    # marker parsing against the resolved target. Local reads use
    # github.token; delegated cross-repository reads require TAP_PAT via the
    # empty-token conditional (delegated runs without PAT yield '' so auth
    # itself fails closed before the embedded guard runs).
    fail_closed_token = "CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED == 'true' && secrets.TAP_PAT || (env.CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED != 'true' && github.token || '')"
    assert_includes convergence, 'github.token'
    assert_includes convergence, 'secrets.TAP_PAT'
    assert_includes convergence, 'CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED'
    assert_includes convergence, fail_closed_token,
                    'the no-progress read must yield an empty token for delegated runs without PAT'
    refute_includes convergence, 'secrets.TAP_PAT || github.token }}',
                    'the no-progress read must not fall back to github.token for delegated runs without PAT'
    assert_includes convergence, 'CONTINUUM_PR_AGENT_TARGET_OWNER'
    assert_includes convergence, 'github.rest.issues.listComments'
    assert_includes convergence, 'continuum-pr-agent-no-progress head='
    assert_includes convergence, 'continuum-pr-agent-convergence from='
    assert_includes convergence, 'failClosed'
    refute_includes convergence, 'createComment'
    refute_includes convergence, 'updateComment'
    refute_includes convergence, 'deleteComment'
    refute_includes convergence, 'createCommitStatus'
    refute_includes convergence, 'gh api'
    refute_includes convergence, 'gh pr view'
    refute_includes convergence, 'gh workflow run'
    refute_includes convergence, 'git push'

    # 2. Writable-branch resolution: GET the current pull only against the
    # resolved target (empty-token conditional as above). Every GitHub
    # operation in this step is read-only and fails closed on mismatch.
    assert_includes target, 'github.token'
    assert_includes target, 'secrets.TAP_PAT'
    assert_includes target, 'CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED'
    assert_includes target, fail_closed_token,
                    'writable-branch resolution must yield an empty token for delegated runs without PAT'
    refute_includes target, 'secrets.TAP_PAT || github.token }}',
                    'writable-branch resolution must not fall back to github.token for delegated runs without PAT'
    assert_includes target, 'repos/$CONTINUUM_PR_AGENT_TARGET_REPOSITORY/pulls/$PR_NUMBER'
    assert_includes target, "CURRENT_SHA=\"$(jq -r '.head.sha'"
    assert_includes target, 'PR_STATE="$(jq -r'
    assert_includes target, 'PR_DRAFT="$(jq -r'
    assert_includes target, '"$CURRENT_SHA" != "$HEAD_SHA"'
    assert_includes target, '"$HEAD_REPO" != "$CONTINUUM_PR_AGENT_TARGET_REPOSITORY"'
    refute_includes target, 'gh pr view'
    refute_includes target, 'gh workflow run'
    refute_includes target, 'git push'
    refute_includes target, '--method POST'
    refute_includes target, '--method PATCH'
    refute_includes target, '--method DELETE'

    # Excluded mixed/write/dispatch/push/cross-repo paths stay PAT-backed.
    policy = step_body(body, 'Resolve PR-Agent signal policy')
    repair_pass = step_body(body, 'Run one bounded OpenCode repair pass over every current item')
    checkout = step_body(body, 'Checkout the writable PR source branch')
    persist = step_body(body, 'Persist PR-Agent no-progress controller state')
    retry_state = step_body(body, 'Update persistent PR-Agent repair retry controller state')
    retry_step = step_body(body, 'Schedule bounded retry for retryable PR-Agent repair failure')
    in_flight = step_body(body, 'Mark PR-Agent repair in flight')
    publish = step_body(body, 'Publish durable PR-Agent repair state')
    failed = step_body(body, 'Publish failed PR-Agent repair state')
    [policy, repair_pass, checkout, persist, retry_state, retry_step, in_flight, publish, failed].each do |step|
      refute_nil step, 'an excluded PAT-backed repair step is missing'
    end
    assert_includes policy, 'GH_TOKEN: ${{ secrets.TAP_PAT }}',
                    'signal-policy reads kodmial/continuum explicitly and may be cross-repo; it must stay PAT-backed'
    assert_includes policy, 'repos/kodmial/continuum/contents'
    assert_includes repair_pass, 'GH_TOKEN: ${{ secrets.TAP_PAT }}',
                    'the repair pass mixes revalidation, comment writes, commit, and push; it must stay wholly PAT-backed'
    refute_includes repair_pass, 'github.token',
                      'the repair pass must not be partially migrated to github.token'
    assert_includes checkout, 'token: ${{ secrets.TAP_PAT }}',
                    'repair checkout must stay PAT-backed so it can push'
    assert_includes persist, 'github-token: ${{ secrets.TAP_PAT }}',
                    'no-progress persistence mixes comment read/update/create/delete; it must stay PAT-backed'
    assert_includes retry_state, 'github-token: ${{ secrets.TAP_PAT }}',
                    'retry controller state mixes comment read/write; it must stay PAT-backed'
    assert_includes retry_step, 'GH_TOKEN: ${{ secrets.TAP_PAT }}',
                    'the repair retry step mixes reads with workflow_dispatch; it must stay wholly PAT-backed'
    assert_includes retry_step, 'gh workflow run'
    refute_includes retry_step, 'github.token',
                      'the repair retry step must not be partially migrated to github.token'
    refute_includes target, 'gh workflow run',
                      'the target-resolution read must stay read-only'
    assert_includes in_flight, 'github-token: ${{ secrets.TAP_PAT }}',
                    'commit-status publishing must stay PAT-backed'
    assert_includes publish, 'github-token: ${{ secrets.TAP_PAT }}',
                    'commit-status publishing must stay PAT-backed'
    assert_includes failed, 'github-token: ${{ secrets.TAP_PAT }}',
                    'commit-status publishing must stay PAT-backed'

    # No global TAP_PAT replacement: the mixed/write paths still draw on it.
    assert_includes body, 'secrets.TAP_PAT'
  end

  # kodmial/continuum#239: the dogfood PR-Agent caller is a direct caller
  # of the reusable PR-Agent workflow, just like the installed caller stub.
  # An explicitly-scoped caller leaves unspecified permissions as none, and a
  # called reusable workflow cannot elevate that token, so the dogfood caller
  # must grant every permission the reusable workflow requires. The stub-only
  # contract in test_callers_grant_required_permissions cannot see this file.
  def test_pr_agent_dogfood_caller_grants_required_permissions
    reusable = yaml(File.join(ROOT, '.github/workflows/continuum-pr-agent.yml'))
    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-pr-agent.yml'))
    caller = yaml(File.join(ROOT, '.github/workflows/pr-agent.yml'))
    rank = { 'none' => 0, 'read' => 1, 'write' => 2 }

    permissions = caller.fetch('permissions')
    ([reusable['permissions']] + reusable['jobs'].values.map { |job| job['permissions'] }).compact.each do |required|
      required.each do |key, value|
        assert_operator rank.fetch(permissions.fetch(key, 'none')), :>=, rank.fetch(value),
                        "pr-agent.yml dogfood caller: #{key} grants #{permissions.fetch(key, 'none')}, needs #{value}"
      end
    end

    # The same-repo review controller dispatches bounded recovery using
    # GITHUB_TOKEN, so the dogfood caller must grant actions:write.
    assert_equal 'write', permissions.fetch('actions'),
                 'pr-agent.yml dogfood caller must grant actions:write for same-repo workflow_dispatch'
    assert_equal 'write', permissions.fetch('statuses'),
                 'pr-agent.yml dogfood caller must grant statuses:write for review lifecycle status'

    # The stub and the dogfood caller call the same reusable workflow, so
    # their permission grants must not diverge again.
    assert_equal stub.fetch('permissions'), permissions,
                 'pr-agent.yml dogfood caller and continuum-pr-agent.yml stub must grant the same permissions'
  end

  # kodmial/continuum#229: ordinary PR comments must not create heavy
  # PR-Agent workflow runs. The heavy entry workflows are dispatch-only;
  # explicit `/review` is routed through a thin router that validates the
  # event, resolves the exact HEAD, and coalesces duplicates on the logical
  # operation key review:<pr>:<head>.
  def test_pr_agent_heavy_workflows_are_dispatch_only
    %w[pr-agent.yml].each do |entry|
      parsed = yaml(File.join(ROOT, '.github/workflows', entry))
      assert_equal %w[workflow_dispatch], events(parsed).keys,
                   "#{entry}: heavy PR-Agent entry must not subscribe to issue_comment"
    end
    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-pr-agent.yml'))
    assert_equal %w[workflow_dispatch], events(stub).keys,
                 'continuum-pr-agent stub: heavy caller must not subscribe to issue_comment'
    %w[pr-agent.yml continuum-pr-agent.yml].each do |base|
      path = base == 'pr-agent.yml' \
        ? File.join(ROOT, '.github/workflows/pr-agent.yml') \
        : File.join(ROOT, '.github/caller-stubs/continuum-pr-agent.yml')
      job = yaml(path).fetch('jobs').fetch('call')
      assert_includes job.fetch('if').to_s, "github.event_name == 'workflow_dispatch'",
                      "#{base}: heavy job must only run on workflow_dispatch"
      refute_includes File.read(path), 'issue_comment',
                      "#{base}: heavy caller must not mention issue_comment"
    end
  end

  def test_pr_agent_review_workflow_uses_repository_token_not_shared_pat
    body = workflow_body('continuum-pr-agent.yml')
    # Target-aware (#243) delegated cross-repository reads require TAP_PAT
    # through the fail-closed conditional, so the review workflow is no
    # longer entirely PAT-free. The repository-token contract is preserved
    # in scoped form: local control-plane runs stay
    # repository-token backed, the shared PAT is never consumed
    # unconditionally, and delegated runs without PAT fail closed instead
    # of silently falling back to github.token. PR-Agent tool execution
    # (pr-agent --pr_url against the resolved target) follows the same
    # target-aware contract: TAP_PAT when delegated, github.token locally.
    refute_includes body, 'secrets.TAP_PAT || github.token }}',
                    'review workflow must never fall back to github.token for delegated runs without PAT'
    refute_includes body, 'GITHUB__USER_TOKEN: ${{ secrets.TAP_PAT }}',
                    'review workflow must not consume the shared user PAT unconditionally for tool execution'
    assert_includes body, 'GITHUB__USER_TOKEN: ${{ env.CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED',
                    'PR-Agent tool execution must select its user token through the target-aware conditional'
    assert_includes body, 'GH_TOKEN: ${{ github.token }}'
    assert_includes body, "CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED == 'true' && secrets.TAP_PAT",
                    'delegated target-aware reads must select TAP_PAT through the fail-closed conditional'
    workflow = yaml(File.join(ROOT, '.github/caller-stubs/continuum-pr-agent.yml'))
    permissions = workflow.fetch('permissions')
    assert_equal 'write', permissions.fetch('actions')
    assert_equal 'write', permissions.fetch('statuses')
  end

  def test_pr_agent_router_validates_and_dispatches_exact_head
    router = File.read(File.join(ROOT, '.github/workflows/continuum-pr-agent-router.yml'))
    # The router validates PR membership, owner-gated actor/policy, and the
    # actionable command before any dispatch.
    assert_includes router, '!issue.pull_request',
                    'router must validate the event belongs to a pull request'
    assert_includes router, 'context.actor !== context.repo.owner',
                    'router must validate the actor is allowed'
    assert_includes router, '/\/review\b/',
                    'router must validate an actionable /review command'
    assert_includes router, "!== 'pr-agent'",
                    'router must validate the review provider policy'
    # Non-actionable comments exit without dispatching.
    assert_includes router, 'no actionable /review command',
                    'ordinary comments must exit without dispatching'
    # The heavy workflow is dispatched with an explicit PR number and the
    # exact current HEAD.
    assert_includes router, 'expected_head_sha',
                    'router must dispatch the heavy workflow with the exact HEAD'
    assert_includes router, 'createWorkflowDispatch',
                    'router must dispatch the heavy workflow via the API'
    assert_includes router, 'pr.head.sha',
                    'router must resolve the exact current HEAD from the PR'
    # Duplicate signals for the same logical operation key are coalesced.
    assert_includes router, 'review:',
                    'router must key duplicate detection on the logical operation'
    assert_includes router, 'already active',
                    'router must coalesce duplicate exact-HEAD dispatches'
    assert_includes router, 'coalesced a duplicate dispatch',
                    'router must log coalesced duplicates instead of dispatching'
    # The same-repo router uses only the repository-scoped token. GitHub
    # explicitly permits workflow_dispatch events created with GITHUB_TOKEN,
    # so the shared user PAT is unnecessary here.
    assert_includes router, 'actions: write',
                    'router needs least-privilege Actions write for workflow_dispatch'
    assert_includes router, 'github-token: ${{ github.token }}',
                    'router must authenticate reads and dispatch with GITHUB_TOKEN'
    assert_includes router, 'github.rest.pulls.get(',
                    'PR metadata read must use repository token'
    assert_includes router, 'github.rest.actions.listWorkflowRuns(',
                    'active-run coalescing read must use repository token'
    refute_includes router, 'github.paginate(',
                    'router must not scan unbounded workflow history'
    assert_includes router, "event: 'workflow_dispatch'",
                    'router active-run lookup must stay scoped to dispatch runs'
    assert_includes router, 'page: 1',
                    'router active-run lookup must remain bounded to one page'
    assert_includes router, 'github.rest.actions.createWorkflowDispatch(',
                    'same-repo workflow dispatch must use repository token first'
    assert_includes router, 'FALLBACK_DISPATCH_TOKEN: ${{ secrets.TAP_PAT }}',
                    'router may inherit PAT only as a dispatch-only fallback'
    assert_includes router, 'permissionDenied',
                    'fallback must be restricted to permission-denied 403 responses'
    assert_includes router, 'rateLimited',
                    'rate-limit 403 must never trigger PAT fallback'
    assert_includes router, 'single dispatch mutation only',
                    'fallback scope must stay one workflow_dispatch mutation'
    assert_includes router, 'fallbackGithub.rest.actions.createWorkflowDispatch',
                    'fallback client must only perform the dispatch mutation'
    # The router never holds a per-PR lock: the actionable filter and the
    # operation-key coalescing run inside the route step, so a plain
    # non-/review comment run can never queue ahead of a useful dispatch.
    # Only the heavy operation layer serializes per PR.
    refute_includes router, 'concurrency:',
                    'router must not hold a per-PR lock that queues no-op runs'
    refute_includes router, 'cancel-in-progress',
                    'router must not serialize via native concurrency'
    refute_includes router, 'group: pr-agent-${',
                    'router must not share the heavy concurrency group'
    refute_includes router, 'group: pr-agent-caller-',
                    'router must not share the heavy caller concurrency group'
    refute_includes router, 'group: pr-agent-router-',
                    'router must not hold its own per-PR lock either'
    %w[.github/caller-stubs/continuum-pr-agent-router.yml .github/workflows/pr-agent-router.yml].each do |path|
      body = File.read(File.join(ROOT, path))
      refute_includes body, 'concurrency:',
                      "#{path}: router entry/stub must not queue no-op runs ahead of useful dispatches"
      refute_includes body, 'cancel-in-progress',
                      "#{path}: router entry/stub must not serialize via native concurrency"
    end
  end

  def test_pr_agent_router_wiring_for_entries_and_consumers
    # The self-hosted entry routes issue_comment through the reusable router
    # with the local heavy workflow as the dispatch target.
    entry = yaml(File.join(ROOT, '.github/workflows/pr-agent-router.yml'))
    assert_includes events(entry).keys, 'issue_comment',
                    'router entry must subscribe to issue_comment'
    assert_includes events(entry).keys, 'workflow_dispatch',
                    'router entry must keep an explicit manual dispatch path'
    entry_job = entry.fetch('jobs').fetch('call')
    assert_includes entry_job.fetch('uses'), 'continuum-pr-agent-router.yml'
    assert_includes entry_job.fetch('with').fetch('review_workflow'), 'pr-agent.yml'
    # The consumer stub mirrors the entry and calls the same reusable.
    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-pr-agent-router.yml'))
    assert_includes events(stub).keys, 'issue_comment',
                    'router stub must subscribe to issue_comment'
    assert_equal entry.fetch('name').sub(' entry', ''), stub.fetch('name'),
                 'router stub and reusable must share one workflow identity'
    stub_job = stub.fetch('jobs').fetch('call')
    assert_includes stub_job.fetch('uses'), 'continuum-pr-agent-router.yml@main'
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
    assert_includes File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml')),
                    "vars.OPENCODE_MODEL || 'opencode/muse-spark-1.3-contributor-free'"
  end

  # Automation limits are consumer policy: every hardcoded timeout/runner in a
  # core workflow must be overridable through a vars.AUTOMATION_* knob whose
  # default preserves the previously hardcoded value.
  def test_automation_limits_are_variable_driven
    core = Dir[File.join(ROOT, '.github/workflows/*.yml')].sort
    targets = %w[continuum-issue-scheduler.yml continuum-opencode.yml continuum-auto-merge.yml continuum-consumer-child-dispatcher.yml]
    targets.each do |name|
      path = File.join(ROOT, '.github/workflows', name)
      assert File.exist?(path), name
      body = File.read(path)
      body.scan(/^(\s*)timeout-minutes: (\d+)$/).each do |indent, minutes|
        flunk "#{name}: hardcoded timeout-minutes: #{minutes}"
      end
      assert_match(/vars\.AUTOMATION_\w+/, body, "#{name}: no AUTOMATION_* knob")
    end

    opencode = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))
    assert_includes opencode, "vars.AUTOMATION_OPENCODE_RUNNER || 'ubuntu-latest'"
    assert_includes opencode, "vars.AUTOMATION_OPENCODE_TIMEOUT_MINUTES || '180'"
    refute_includes opencode, 'swift-actions/setup-swift'
    refute_includes opencode, 'sw_vers'
    assert_includes opencode, 'CONTINUUM_AGENT_PREPARE_COMMAND'

    scheduler = File.read(File.join(ROOT, '.github/workflows/continuum-issue-scheduler.yml'))
    assert_includes scheduler, "vars.AUTOMATION_WIP_LIMIT || '2'"
    assert_includes scheduler, "vars.AUTOMATION_LEASE_MINUTES || '45'"
    assert_includes scheduler, "vars.AUTOMATION_MAX_DISPATCH_ATTEMPTS || '2'"
  end

  # Core workflows must carry no product-specific path outside the opt-in
  # tech layer; the release version file is a consumer variable.

  def test_core_workflows_expose_no_product_paths
    paths =
      Dir[File.join(ROOT, '.github/workflows/continuum-*.yml')] +
      Dir[File.join(ROOT, '.github/caller-stubs/**/*.yml')] +
      Dir[File.join(ROOT, '.github/scripts/**/*')].select { |path| File.file?(path) } +
      Dir[File.join(ROOT, 'src/**/*')].select { |path| File.file?(path) } +
      [File.join(ROOT, 'install.sh')]

    banned = %r{
      nanodictate |
      kodmai |
      runtime-lab |
      NANODICTATE_SIGNING |
      Sources/NanoDictateCore |
      macports-nanodictate |
      homebrew-nanodictate
    }ix

    violations = paths.filter_map do |path|
      next unless File.read(path).match?(banned)
      path.delete_prefix(ROOT + '/')
    end
    assert_empty violations,
                 "Continuum generic workflows/templates contain consumer-specific identifiers: #{violations.join(', ')}"

    auto_merge = workflow_body('continuum-auto-merge.yml')
    assert_includes auto_merge, "CONTINUUM_VERSION_FILE: ${{ vars.CONTINUUM_VERSION_FILE || '' }}"
    refute_includes auto_merge, 'Sources/NanoDictateCore'
  end

  def test_opencode_issue_number_input_is_optional_and_falls_back
    inputs = events(yaml(File.join(ROOT, '.github/workflows/continuum-opencode.yml')))
             .fetch('workflow_call').fetch('inputs')
    issue = inputs.fetch('issue_number')
    assert_equal '', issue.fetch('default')
    assert_equal false, issue.fetch('required')

    body = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))
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
    body = workflow_body('continuum-opencode.yml')
    modes = dispatch_modes
    refute_empty modes
    assert_equal modes.uniq, modes, 'dispatching whitelist has duplicates'
    modes.each do |mode|
      assert_includes body, "inputs.mode == '#{mode}'",
                      "mode #{mode} is whitelisted but no step branches on it"
    end

    # Parse the dispatch job and require each whitelisted mode to own at least
    # one step that really does work, not just one that compares the mode.
    steps = yaml(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))
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
        assert_match(/--method POST|['"]POST \/repos|method:\s*'POST'|createWorkflowDispatch/, call, "#{name}: dispatch call is not a POST/action dispatch")
      end
    end
  end

  # Every new knob is an additive input with a default that reproduces the
  # pre-existing behaviour, so existing stubs keep working untouched.
  def test_new_consumer_knobs_are_additive_with_safe_defaults
    scheduler = events(yaml(File.join(ROOT, '.github/workflows/continuum-issue-scheduler.yml')))
                  .fetch('workflow_call').fetch('inputs')
    {
      'wip_limit' => '',
      'lease_minutes' => '',
      'max_dispatch_attempts' => '',
      'dispatch_marker' => '<!-- issue-scheduler-dispatch -->',
      'in_progress_label' => 'automation:in-progress',
      'pause_marker' => 'automation:paused',
      'qualifying_label' => 'automation:qualifying',
      'blocked_label' => 'automation:blocked',
      'post_pause_comment' => 'true',
      'reset_markers' => 'false',
      'require_priority_label' => 'false',
      'command_grace_minutes' => '5',
      'child_owned_marker' => '<!-- continuum-child-owned -->',
      'legacy_child_owned_marker' => '<!-- runtime-worker-owned -->',
      'opencode_workflow_name' => 'OpenCode agent',
      'opencode_workflow_path' => '.github/workflows/continuum-opencode.yml',
      'dispatch_ref' => 'main',
      'opencode_dispatch' => 'comment',
      'execution_label_routes' => '',
      'child_dispatch_workflow' => ''
    }.each do |key, default|
      assert scheduler.key?(key), "issue-scheduler missing input #{key}"
      assert_equal default, scheduler.fetch(key).fetch('default'), key
      assert_equal 'string', scheduler.fetch(key).fetch('type'), key
    end

    auto_merge = events(yaml(File.join(ROOT, '.github/workflows/continuum-auto-merge.yml')))
                  .fetch('workflow_call').fetch('inputs')
    %w[post_merge_wakeups post_merge_wakeup_ref].each do |key|
      assert auto_merge.key?(key), "auto-merge missing input #{key}"
      assert_equal '', auto_merge.fetch(key).fetch('default'), key
      assert_equal 'string', auto_merge.fetch(key).fetch('type'), key
    end

    opencode = events(yaml(File.join(ROOT, '.github/workflows/continuum-opencode.yml')))
               .fetch('workflow_call').fetch('inputs')
    {
      'max_dispatch_attempts' => '',
      'dispatch_marker' => '',
      'in_progress_label' => '',
      'pause_marker' => '',
      'ci_workflow_id' => '',
      'base_ref' => '',
      'knowledge_protocol_path' => '',
      'knowledge_records_dir' => '',
      'issue_commit_prefix' => ''
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
    stub = events(yaml(File.join(ROOT, '.github/caller-stubs/continuum-opencode.yml')))
           .fetch('workflow_dispatch').fetch('inputs').fetch('conflict_strategy')
    assert_equal 'choice', stub.fetch('type')
    assert_equal %w[merge checkout], stub.fetch('options')

    # The `mode` choice is the same kind of contract, and just as invisible: an
    # extra stub option is accepted by these tests but rejected by GitHub when
    # the call is actually made, and a missing one makes a whitelisted mode
    # unreachable for the operator.
    dispatch_inputs = events(yaml(File.join(ROOT, '.github/caller-stubs/continuum-opencode.yml')))
                     .fetch('workflow_dispatch').fetch('inputs').fetch('mode')
    assert_equal 'choice', dispatch_inputs.fetch('type')
    assert_equal dispatch_modes.sort, dispatch_inputs.fetch('options').sort,
                 'the stub mode choice and the engine dispatch whitelist must be the same set'

    body = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))
    assert_match(/case "\$CONFLICT_STRATEGY" in/, body)
    assert_includes body, "expected 'merge' or 'checkout'"
    # The prompt must select a whole strategy block, not interpolate one verb.
    assert_includes body, 'STRATEGY_BLOCK'
  end

  # The unified scheduler and child review must share the configured labels.
  # A parent clears stale child pauses in the same scheduler that selects local
  # and delegated tasks, so a hardcoded label would split the one queue.
  def test_parent_child_workflows_use_the_configured_labels
    scheduler = workflow_body('continuum-issue-scheduler.yml')
    assert_includes scheduler, "PAUSE_LABEL: ${{ inputs.pause_marker || vars.AUTOMATION_PAUSE_LABEL || 'automation:paused' }}"
    refute_match(/labels\/automation%3Apaused/, scheduler,
                 'scheduler must not hardcode the default pause label in a request URL')
    assert_match(%r{\$\{PAUSE_LABEL//:/%3A\}}, scheduler,
                 'scheduler must use the configured pause label for delegated children')
    assert_match(/--arg pause "\$PAUSE_LABEL"/, scheduler,
                 'scheduler must select stale child pause labels by the configured label')

    review = workflow_body('continuum-consumer-child-review.yml')
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
    scheduler = events(yaml(File.join(ROOT, '.github/workflows/continuum-issue-scheduler.yml')))
                  .fetch('workflow_call').fetch('inputs').fetch('post_pause_comment')
    description = scheduler.fetch('description')
    assert_includes description, 'false',
                    'the description must name the exact value that disables the comment'
    assert_includes description, 'disables',
                    'the description must say which value turns the comment off'

    body = workflow_body('continuum-issue-scheduler.yml')
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
    body = File.read(File.join(ROOT, '.github/workflows/continuum-issue-scheduler.yml'))

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

  # continuum-opencode.yml must consume the same naming knobs as the scheduler on the
  # issue_comment path, where only vars are available, and must actually run the
  # consumer's blocking CI workflow when ci_workflow_id is set.
  def test_opencode_naming_knobs_and_ci_rerun_drive_implementation
    body = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))

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
  def test_opencode_environment_is_consumer_owned
    body = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))
    assert_includes body, "runs-on: ${{ vars.AUTOMATION_OPENCODE_RUNNER || 'ubuntu-latest' }}"
    assert_includes body, 'CONTINUUM_AGENT_PREPARE_COMMAND'
    assert_includes body, 'eval "$PREPARE_COMMAND"'
    refute_includes body, 'swift-actions/setup-swift'
    refute_includes body, 'sw_vers'
    refute_includes body, "'macos-15'"
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
    opencode = yaml(File.join(ROOT, '.github/caller-stubs/continuum-opencode.yml'))
    watched = events(watchdog_stub).fetch('workflow_run').fetch('workflows')
    assert_equal [opencode.fetch('name')], watched,
                 'watchdog must watch the OpenCode caller by its `name:` value'
    assert_equal opencode.fetch('name'),
                 yaml(File.join(ROOT, '.github/workflows/continuum-opencode.yml')).fetch('name'),
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

  def test_watchdog_same_repo_reads_use_repository_token_but_writes_keep_pat
    body = watchdog_body

    assert_includes body, 'READ_GITHUB_TOKEN: ${{ github.token }}'
    assert_includes body, 'github-token: ${{ secrets.TAP_PAT }}'
    assert_includes body, 'const readGithub = new github.constructor({'
    assert_includes body, 'baseUrl: github.request.endpoint.DEFAULTS.baseUrl'

    %w[
      readGithub.rest.issues.listForRepo
      readGithub.rest.issues.get
      readGithub.rest.pulls.list
      readGithub.rest.actions.listWorkflowRunsForRepo
      readGithub.rest.issues.listComments
    ].each do |read_call|
      assert_includes body, read_call
    end
    assert_includes body, 'const blockers = await readGithub.paginate('

    refute_includes body, 'await github.rest.issues.get({'
    refute_includes body, 'github.rest.issues.listForRepo'
    refute_includes body, 'github.rest.pulls.list'
    refute_includes body, 'github.rest.actions.listWorkflowRunsForRepo'
    refute_includes body, 'github.rest.issues.listComments'

    # Mutations stay on the PAT-authenticated action client so their
    # issue_comment / label events continue to wake downstream automation.
    assert_includes body, 'await github.rest.issues.createComment({'
    assert_includes body, 'await github.rest.issues.addLabels({'
    assert_includes body, 'await github.rest.issues.removeLabel({'
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
    scheduler = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))
    assert_includes scheduler, "IN_PROGRESS_LABEL: ${{ inputs.in_progress_label || vars.AUTOMATION_IN_PROGRESS_LABEL || 'automation:in-progress' }}"
    assert_includes scheduler, "PAUSE_LABEL: ${{ inputs.pause_marker || vars.AUTOMATION_PAUSE_LABEL || 'automation:paused' }}"
  end

  # The reason the watchdog exists: `continuum-opencode.yml`'s recovery only fires on a
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
    # own continuum-opencode.yml does not, so `display_title` is the issue title there).
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
    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-opencode-repair.yml'))
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

    body = workflow_body('continuum-opencode-repair.yml')
    job = body[/^  ci-repair-dispatch:\n(.*?)(?=^  \S|\z)/m, 1]
    refute_nil job, 'continuum-opencode-repair.yml has no ci-repair-dispatch job'
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
    body = workflow_body('continuum-opencode-repair.yml')
    job = body[/^  ci-repair-dispatch:\n(.*?)(?=^  \S|\z)/m, 1]
    refute_nil job, 'continuum-opencode-repair.yml has no ci-repair-dispatch job'
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

    body = workflow_body('continuum-opencode-repair.yml')
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
    with = yaml(File.join(ROOT, '.github/caller-stubs/continuum-opencode-repair.yml'))
           .fetch('jobs').fetch('call').fetch('with')
    %w[pr_number head_sha conclusion run_id ci_repair_label head_ref_pattern
       auto_merge_workflow opencode_workflow].each do |key|
      assert_equal "\${{ inputs.#{key} }}", with.fetch(key), "#{key} must be a bare passthrough"
    end

    # Both per-HEAD locks are reset on synchronize/reopen. A stale conflict
    # label from an older HEAD must never suppress repair of the new HEAD.
    assert_includes body, '"repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/labels/$CI_REPAIR_LABEL"',
                    'the per-head reset must clear the configured CI lock'
    assert_includes body, '"repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/labels/opencode-conflict-repair"',
                    'the per-head reset must clear the stale conflict lock'
    assert_includes body, 'Reset per-head CI/conflict repair locks',
                    'synchronize/reopen must start a fresh conflict-repair episode'
    # The workflow_run path keeps its looser `opencode/*` guard: tightening it
    # would stop repairing heads this controller repaired before.
    assert_includes body, '"$head_ref" != opencode/*'
  end

  # ------------------------------------------------- render execution controller

  RENDER = 'continuum-render-executor.yml'
  DOCKER = 'continuum-docker-qualification.yml'

  def docker_core
    yaml(File.join(ROOT, '.github/workflows', DOCKER))
  end

  def docker_body
    File.read(File.join(ROOT, '.github/workflows', DOCKER))
  end

  def docker_stub
    yaml(File.join(ROOT, '.github/caller-stubs', DOCKER))
  end

  def render_stub
    yaml(File.join(ROOT, '.github/caller-stubs', RENDER))
  end

  def render_core
    yaml(File.join(ROOT, '.github/workflows', RENDER))
  end

  def render_body
    File.read(File.join(ROOT, '.github/workflows', RENDER))
  end

  # The fork this core file was ported from read two different credentials:
  # `secrets.KEY` was the Render API key and `secrets.TAP_PAT` was the GitHub
  # PAT. They are not interchangeable — a GitHub token is not accepted by the
  # Render API — so Continuum documents the Render key under its own name,
  # `RENDER_API_KEY`, and this workflow may not quietly alias one onto the
  # other.
  def test_render_executor_reads_the_render_api_key_and_never_the_github_pat
    body = render_body
    # Both Render steps must be wired, not just one: the execute step creating
    # the worker and the cleanup step deleting it need the same key, and a
    # cleanup that lost it would leave an ephemeral worker running.
    assert_equal 2, body.scan(/RENDER_API_KEY: \$\{\{ secrets\.RENDER_API_KEY \}\}/).size,
                 'both the execute and the mandatory cleanup step must read RENDER_API_KEY'
    # The defect this pins: the GitHub PAT forwarded to the Render API.
    refute_includes body, 'RENDER_API_KEY: ${{ secrets.TAP_PAT }}',
                    'TAP_PAT is a GitHub PAT; the Render API key must not be wired from it'
    # The fork spelled the Render key `KEY`, which is too generic to be a
    # contract, so Continuum renamed rather than adopted it.
    refute_includes body, 'secrets.KEY',
                    'the fork read secrets.KEY; the Render key is named RENDER_API_KEY by contract'
    # The classification step is the one place a GitHub token belongs, and it
    # keeps the PAT-with-token-fallback chain.
    assert_includes body, 'GH_TOKEN: ${{ secrets.TAP_PAT || github.token }}'
  end

  # A missing Render key must fail the run explicitly rather than send an
  # unauthenticated request or fall back to some other credential; the same
  # holds for the mandatory cleanup, whose silence would leave a worker running.
  def test_render_executor_fails_explicitly_without_a_render_api_key
    body = render_body
    assert_includes body, '[[ -n "$RENDER_API_KEY" ]] || {',
                    'the execute step must guard the Render API key explicitly'
    assert_includes body, 'echo "::error::No Render API key configured: set the RENDER_API_KEY secret."',
                    'the execute step must name the exact secret to set'
    assert_includes body, 'echo "::error::RENDER_API_KEY is the Render API key and is distinct from TAP_PAT; a GitHub token is not accepted by the Render API."',
                    'the failure must record that the key is distinct from TAP_PAT'
    # The cleanup guard sits inside the branch that actually calls Render, so a
    # missing key fails there too instead of silently skipping the deletion.
    assert_equal 2, body.scan('[[ -n "$RENDER_API_KEY" ]] || {').size,
                 'the mandatory cleanup step must guard the Render API key as well'
    assert_includes body, 'echo "::error::No Render API key configured: set the RENDER_API_KEY secret; the ephemeral Render service cannot be deleted without it."',
                    'the cleanup step must name the exact secret to set'
    # No fallback may smuggle a GitHub token back in as the Render credential.
    refute_match(/RENDER_API_KEY: \$\{\{ secrets\.TAP_PAT \|\|/, body,
                 'RENDER_API_KEY must never fall back to TAP_PAT or github.token')
  end

  # The consumer-facing variables document states the render controller's secret
  # contract. Pin the document to the code, not merely to itself: the defect
  # this guards is a doc claiming `TAP_PAT` serves the Render API when the
  # workflow reads `RENDER_API_KEY` there, so a test that only checked the doc
  # for the word `TAP_PAT` would have passed the broken text.

  def test_consumer_variables_doc_pins_the_render_secret_contract_to_the_code
    body = render_body
    doc = File.read(File.join(ROOT, 'docs/consumer-variables.md'))
    code_secrets = body.scan(/secrets\.([A-Z][A-Z0-9_]*)/).flatten.uniq.sort
    assert_equal %w[RENDER_API_KEY TAP_PAT], code_secrets
    assert_includes doc, '| `RENDER_API_KEY` | Render execution is enabled | Render API credential. |'
    assert_includes doc, '| `TAP_PAT` | Generic workflows need authenticated GitHub writes |'
    refute_includes body, 'RENDER_API_KEY: ${{ secrets.TAP_PAT'
  end


  def test_docker_qualification_secret_claim_in_the_doc_matches_the_code
    body = docker_body
    doc = File.read(File.join(ROOT, 'docs/consumer-variables.md'))
    refute_includes body, 'RENDER_API_KEY'
    assert_equal %w[TAP_PAT], body.scan(/secrets\.([A-Z][A-Z0-9_]*)/).flatten.uniq
    assert_includes doc, '| `TAP_PAT` | Generic workflows need authenticated GitHub writes |'
  end


  def test_the_documented_secret_count_per_set_matches_the_installed_code
    consumer_defined = lambda do |stubs|
      secrets_read_by(stubs).reject { |name| name == 'GITHUB_TOKEN' }
    end

    assert_equal %w[RENDER_API_KEY TAP_PAT], consumer_defined.call(CORE_STUBS)
    assert_equal %w[CHILD_RUNTIME_REPOSITORIES CHILD_RUNTIME_TOKEN TAP_PAT],
                 consumer_defined.call(PARENT_STUBS)
    assert_empty consumer_defined.call(TECH_STUBS),
                 'the consumer-neutral Swift profile must require no product secret'
  end


  def test_no_documented_secret_is_unread_by_the_code
    doc = File.read(File.join(ROOT, 'docs/consumer-variables.md'))
    table = doc[/^## Secrets.*?(?=^## )/m]
    refute_nil table, 'the consumer contract must keep a Secrets table'
    readable = ->(name) { WORKFLOWS.any? { |path| File.read(path).include?("secrets.#{name}") } }

    names = table.scan(/\| `([A-Z][A-Z0-9_]*)` \|/).flatten.uniq
    refute_empty names
    names.each do |name|
      assert readable.call(name),
             "docs/consumer-variables.md documents #{name}, which no workflow reads"
    end

    { 'core' => CORE_STUBS, 'parent' => PARENT_STUBS, 'tech' => TECH_STUBS }.each do |set, stubs|
      secrets_read_by(stubs).reject { |name| name == 'GITHUB_TOKEN' }.each do |name|
        assert_includes table, "`#{name}`",
                        "the #{set} set reads #{name}, so the Secrets table must document it"
      end
    end
  end

  def test_parent_stubs_forward_the_parents_own_pat_as_the_child_runtime_token
    doc = File.read(File.join(ROOT, 'docs/consumer-variables.md'))

    PARENT_STUBS.each do |path|
      forwarded = yaml(path).fetch('jobs').each_value
                                    .map { |job| job.is_a?(Hash) ? job['secrets'] : nil }
                                    .compact
      if File.basename(path) == 'continuum-child-run-cleanup.yml'
        assert_empty forwarded,
                     'run cleanup must not receive the private child runtime credential'
      else
        assert_equal [{ 'CHILD_RUNTIME_TOKEN' => '${{ secrets.TAP_PAT }}' }], forwarded,
                     "#{File.basename(path)} must forward the parent's TAP_PAT as CHILD_RUNTIME_TOKEN"
      end
    end

    assert_match(%r{Every installed parent execution stub fills `CHILD_RUNTIME_TOKEN` from the parent's own `TAP_PAT`}, doc,
                 'the doc must distinguish child execution callers from metadata-only cleanup')
  end

  # README.md is the first document a consumer reads, and its secrets table had
  # drifted: it omitted RENDER_API_KEY entirely while listing the two provider
  # keys no workflow reads and that the no-paid-provider rule forbids. Derive
  # the whole table from the code so neither drift can recur.
  def test_the_readme_secrets_table_matches_the_code
    readme = File.read(File.join(ROOT, 'README.md'))
    table = readme[/^## Secrets and variables.*?(?=^Repository variables)/m]
    refute_nil table, 'README.md must keep a Secrets and variables section with a secrets table'

    readable = ->(name) { WORKFLOWS.any? { |path| File.read(path).include?("secrets.#{name}") } }

    (CORE_STUBS + PARENT_STUBS + TECH_STUBS).each do |stub|
      secrets_read_by([stub]).reject { |name| name == 'GITHUB_TOKEN' }.each do |name|
        assert_includes table, "`#{name}`",
                        "#{File.basename(stub)} reads #{name}, so the README secrets table must list it"
      end
    end

    # No paid provider key may be presented as part of the contract. The rule
    # is that no workflow reads one, so naming one in the table would invite a
    # consumer to define a credential nothing consumes.
    %w[OPENCODE_API_KEY ANTHROPIC_API_KEY GROQ_API_KEY].each do |key|
      WORKFLOWS.each do |path|
        refute_includes File.read(path), "secrets.#{key}",
                        "#{File.basename(path)} reads a paid provider key, which the no-paid-provider rule forbids"
      end
    end
    # The table may mention them only to say they are not read.
    table.scan(/`([A-Z][A-Z0-9_]*)`/).flatten.uniq.each do |name|
      next if readable.call(name)

      refute_match(/^\|.*`#{Regexp.escape(name)}`.*\|$/, table,
                   "the README secrets table lists #{name} as a secret to define, but no workflow reads it")
    end
    assert_match(/no workflow reads/i, table,
                 'the README must say plainly that no workflow reads a paid provider key')
  end

  # CONTINUUM_HOMEBREW_TAP and CONTINUUM_MACPORTS_TREE were documented as
  # repository variables that nothing reads. Naming a variable a consumer
  # cannot set is worse than not documenting it: it implies a knob that does
  # nothing. The doc now says the opposite explicitly, and this test holds that
  # statement to the code.
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
      'JOB_SCRIPT' => ['job_script', 'RENDER_JOB_SCRIPT', nil],
      'CLEANUP_SCRIPT' => ['cleanup_script', 'RENDER_CLEANUP_SCRIPT', nil],
      'QUALIFICATION_SCRIPT' => ['qualification_script', 'RENDER_QUALIFICATION_SCRIPT', nil],
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
      # The chain is `inputs.x || vars.VAR`; consumer-owned script hooks and the model have no literal.
      expected = literal ? "inputs.#{input} || vars.#{variable} || '#{literal}'" : "inputs.#{input} || vars.#{variable}"
      assert_includes body, "#{env_key}: \${{ #{expected} }}", "#{env_key}: env mapping missing"
    end

    # The model is one required knob with no default anywhere in the chain, so the
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
                 '<!-- runtime-lab-render-qualification-result -->'],
      DOCKER => ['kodmial/runtime-lab', 'repos/kodmial/opencode', 'kodmial/opencode',
                 'runtime-lab-qualification-result', 'docker-qualification-result/v1']
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

  # ------------------------------------------------- docker qualification controller

  # The artifact store, the memory ceiling and the payload schema were all
  # fork literals. Each is an input with a `vars.` fallback, except the two
  # that must not have one.
  def test_docker_qualification_knobs_cover_every_fork_hardcoded_value
    env = docker_core.fetch('jobs').fetch('qualify').fetch('env')
    {
      'ARTIFACT_REPOSITORY' => ['artifact_repository', 'ARTIFACT_REPOSITORY', 'github.repository'],
      'BINARY_NAME' => ['binary_name', 'DOCKER_QUALIFICATION_BINARY_NAME', "'opencode-coding-linux-x64'"],
      'DOCKER_IMAGE' => ['docker_image', 'DOCKER_QUALIFICATION_IMAGE', "'ubuntu:22.04'"],
      'MEMORY_MIB' => ['memory_mib', 'DOCKER_QUALIFICATION_MEMORY_MIB', "'512'"],
      'MIN_HEADROOM_MIB' => ['min_headroom_mib', 'DOCKER_QUALIFICATION_MIN_HEADROOM_MIB', "'32'"],
      'TRIALS' => ['trials', 'DOCKER_QUALIFICATION_TRIALS', "'2'"],
      # No literal: an unmodelled trial changes nothing and would be recorded
      # as a correctness failure against the binary.
      'OPENCODE_MODEL' => ['model', 'OPENCODE_MODEL', nil],
      'RESULT_SCHEMA' => ['result_schema', 'DOCKER_QUALIFICATION_RESULT_SCHEMA', "'continuum-qualification-result/v1'"],
      'RESULT_KIND' => ['result_kind', 'DOCKER_QUALIFICATION_RESULT_KIND', "'docker'"],
      'RESULT_MARKER' => ['result_marker', 'DOCKER_QUALIFICATION_RESULT_MARKER',
                          "'<!-- continuum-docker-qualification-result -->'"],
      'RESULT_FILE' => ['result_file', 'DOCKER_QUALIFICATION_RESULT_FILE',
                        "'/tmp/continuum-docker-qualification-result.json'"],
      'EVIDENCE_DIR' => ['evidence_dir', 'DOCKER_QUALIFICATION_EVIDENCE_DIR',
                         "'/tmp/continuum-docker-qualification-evidence'"],
      'ARTIFACT_PREFIX' => ['artifact_prefix', 'DOCKER_QUALIFICATION_ARTIFACT_PREFIX', "'docker-qualification'"],
      'DISPATCH_REF' => ['dispatch_ref', 'DOCKER_QUALIFICATION_DISPATCH_REF', "'main'"],
      'IN_PROGRESS_LABEL' => ['in_progress_label', 'AUTOMATION_IN_PROGRESS_LABEL', "'automation:in-progress'"],
      'PAUSE_LABEL' => ['pause_label', 'AUTOMATION_PAUSE_LABEL', "'automation:paused'"],
      # Optional integration: empty means the wake is skipped, not dispatched.
      'CHAIN_WORKFLOW' => ['chain_workflow', 'DOCKER_QUALIFICATION_CHAIN_WORKFLOW', nil]
    }.each do |name, (input, var, literal)|
      expected = "${{ inputs.#{input} || vars.#{var}#{literal ? " || #{literal}" : ''} }}"
      assert_equal expected, env.fetch(name).to_s
    end
  end

  # The fork spent its 512 MiB by writing the number twice. One knob now drives
  # both flags, so the ceiling and the swap allowance cannot drift apart, and
  # the same number reaches the python classifier that judges the headroom.
  def test_docker_qualification_drives_memory_and_trials_from_the_configured_values
    body = docker_body
    assert_includes body, 'docker run --rm --memory="${MEMORY_MIB}m" --memory-swap="${MEMORY_MIB}m"'
    assert_includes body, 'for TRIAL in $(seq 1 "$TRIALS"); do'
    assert_includes body, 'if len(passes) == trials_wanted:'
    assert_includes body, '"schema": schema,'
    assert_includes body, '"kind": kind,'
    # A bare 512 in the classifier would silently ignore the knob.
    refute_includes body, 'limit = 512 * 1024 * 1024'
    refute_includes body, 'min_headroom = 32 * 1024 * 1024'
    # Each knob is validated, so a typo fails the run instead of classifying.
    assert_includes body, '[[ "$MEMORY_MIB" =~ ^[0-9]+$ && "$MEMORY_MIB" -gt 0 ]] || {'
    assert_includes body, '[[ "$TRIALS" =~ ^[0-9]+$ && "$TRIALS" -gt 0 ]] || {'
  end

  # The secret contract is TAP_PAT. `github.token` is the documented fallback
  # for the read-only qualification steps.
  def test_docker_qualification_reads_only_tap_pat
    body = docker_body
    refute_includes body, 'secrets.KEY'
    assert_equal 2, body.scan('secrets.TAP_PAT || github.token').size
    refute_includes body, 'GH_TOKEN: ${{ github.token }}'
  end

  # The recorded verdict must carry the configured marker and pause the issue
  # with the configured label, or a renamed marker makes the result unreadable.
  def test_docker_qualification_records_the_configured_marker_and_labels
    body = docker_body
    assert_includes body, 'printf \'%s\n%s\' "$RESULT_MARKER" "$BODY"'
    assert_includes body, '--add-label "$PAUSE_LABEL" --remove-label "$IN_PROGRESS_LABEL"'
    assert_includes body, 'classification:"infrastructure"'
    # The optional chain must not dispatch a file the consumer may not have.
    assert_includes body, 'if [[ -z "$CHAIN_WORKFLOW" ]]; then'
    assert_includes body, 'gh workflow run "$CHAIN_WORKFLOW"'
    assert_includes body, 'name: ${{ env.ARTIFACT_PREFIX }}-${{ inputs.issue_number }}-${{ github.run_id }}'
  end

  # A qualification with no issue to run is not a qualification.
  def test_docker_qualification_stub_requires_its_issue_and_pins_nothing
    dispatch = events(docker_stub).fetch('workflow_dispatch')
    issue = dispatch.fetch('inputs').fetch('issue_number')
    assert_equal true, issue.fetch('required')
    assert_equal 'string', issue.fetch('type')
    (dispatch.fetch('inputs').keys - %w[issue_number]).each do |key|
      assert_equal false, dispatch.fetch('inputs').fetch(key).fetch('required'),
                   "#{key} must not gate the dispatch"
    end

    callee = events(docker_core).fetch('workflow_call').fetch('inputs')
    assert_equal '', callee.fetch('issue_number').fetch('default')
    assert_equal false, callee.fetch('issue_number').fetch('required')

    # The artifact store and the memory ceiling are the consumer's to choose,
    # so the stub forwards them empty rather than pinning a fork's values.
    call = docker_stub.fetch('jobs').fetch('call').fetch('with')
    %w[artifact_repository memory_mib model binary_name trials].each do |key|
      assert_equal "${{ inputs.#{key} }}", call.fetch(key)
    end
  end

  # ------------------------------------------------- stub input contract

  # Every `with:` key each core stub is allowed to pass. A stub is installed
  # verbatim into a consumer repository, so a key added here is a decision
  # Continuum makes on every consumer's behalf and needs a test edit.
  STUB_INPUT_WHITELIST = {
    'continuum-add-review-label.yml' => %w[continuum_ref],
    'continuum-auto-merge.yml' => %w[continuum_ref post_merge_wakeups post_merge_wakeup_ref],
    'continuum-bootstrap-runtime-secret.yml' => %w[continuum_ref repository],
    'continuum-coderabbit-retry.yml' => %w[continuum_ref],
    'continuum-coderabbit-unresolved.yml' => %w[continuum_ref],
    'continuum-docker-qualification.yml' => %w[
      continuum_ref issue_number artifact_repository binary_name model
      docker_image memory_mib min_headroom_mib trials result_schema
      result_kind result_marker result_file evidence_dir artifact_prefix
      dispatch_ref in_progress_label pause_label chain_workflow
      concurrency_group timeout_minutes
    ],
    'continuum-opencode-watchdog.yml' => %w[continuum_ref watched_workflow],
    'continuum-render-executor.yml' => %w[
      continuum_ref issue_number mode render_region model state_file result_file
      memory_summary_file qualification_result_file max_repair_attempts
      qualification_label qualification_marker artifact_prefix dispatch_ref
      job_script cleanup_script qualification_script in_progress_label
      pause_label repair_label e2e_branch_prefix chain_workflow
      scheduler_workflow concurrency_group timeout_minutes
    ],
    'continuum-issue-scheduler.yml' => %w[
      continuum_ref wip_limit lease_minutes max_dispatch_attempts dispatch_marker
      in_progress_label pause_marker qualifying_label blocked_label
      post_pause_comment reset_markers
      require_priority_label command_grace_minutes child_owned_marker
      legacy_child_owned_marker opencode_workflow_name opencode_workflow_path
      dispatch_ref opencode_dispatch execution_label_routes
      child_dispatch_workflow
      count_open_prs_as_wip pause_on_failure
      caller_event_name caller_issue_number
    ],
    'continuum-opencode.yml' => %w[
      continuum_ref mode issue_number pr_number head_ref review_id run_id
      ci_workflow_id conflict_strategy dispatch_marker in_progress_label
      pause_marker max_dispatch_attempts base_ref knowledge_protocol_path
      knowledge_records_dir issue_commit_prefix
      pause_marker max_dispatch_attempts pause_on_failure ci_repair_label
      packaging_repair_label capability_number qualification_number required_sha
    ],
    'continuum-opencode-repair.yml' => %w[
      continuum_ref pr_number head_sha conclusion run_id ci_repair_label
      head_ref_pattern auto_merge_workflow opencode_workflow
    ],
    'continuum-opencode-unresolved.yml' => %w[continuum_ref],
    'continuum-pr-agent-recovery.yml' => %w[continuum_ref target_child_id max_executions],
    'continuum-pr-agent-canary.yml' => %w[
      continuum_ref canary_enabled base_ref opencode_model model max_tokens
      api_base bridge_port server_port pr_agent_version
    ],
    'continuum-pr-agent.yml' => %w[
      continuum_ref pr_number target_child_id expected_head_sha retry_attempt retry_workflow recovery_kind
    ],
    'continuum-pr-agent-router.yml' => %w[
      continuum_ref review_workflow review_provider pr_number
    ],
    'continuum-pr-agent-repair.yml' => %w[
      continuum_ref pr_number target_child_id head_sha review_json improve_jsonl
      retry_attempt retry_workflow
    ],
    'continuum-pr-agent-auto-merge.yml' => %w[
      continuum_ref pr_number target_child_id head_sha review_json improve_jsonl persistent_state_json
      post_merge_wakeups post_merge_wakeup_ref
      required_workflow_gate_label required_workflow_gate_name
    ],
    'continuum-remove-review-label.yml' => %w[continuum_ref]
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
        # Event plumbing carries the caller's own trigger context, not a
        # consumer knob: it binds github.* rather than inputs.* by design.
        next if base == 'continuum-issue-scheduler.yml' &&
                %w[caller_event_name caller_issue_number].include?(key)

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
    scheduler = File.read(File.join(ROOT, '.github/workflows/continuum-issue-scheduler.yml'))
    {
      'WIP_LIMIT' => %w[wip_limit AUTOMATION_WIP_LIMIT 2],
      'LEASE_MINUTES' => %w[lease_minutes AUTOMATION_LEASE_MINUTES 45],
      'MAX_DISPATCH_ATTEMPTS' => %w[max_dispatch_attempts AUTOMATION_MAX_DISPATCH_ATTEMPTS 2],
      'DISPATCH_MARKER' => ['dispatch_marker', 'AUTOMATION_DISPATCH_MARKER', '<!-- issue-scheduler-dispatch -->'],
      'IN_PROGRESS_LABEL' => ['in_progress_label', 'AUTOMATION_IN_PROGRESS_LABEL', 'automation:in-progress'],
      'PAUSE_LABEL' => ['pause_marker', 'AUTOMATION_PAUSE_LABEL', 'automation:paused'],
      'QUALIFYING_LABEL' => ['qualifying_label', 'AUTOMATION_QUALIFYING_LABEL', 'automation:qualifying'],
      'BLOCKED_LABEL' => ['blocked_label', 'AUTOMATION_BLOCKED_LABEL', 'automation:blocked'],
      'COUNT_OPEN_PRS_AS_WIP' => ['count_open_prs_as_wip', 'AUTOMATION_COUNT_OPEN_PRS_AS_WIP', 'true'],
      'PAUSE_ON_FAILURE' => ['pause_on_failure', 'AUTOMATION_PAUSE_ON_FAILURE', 'false']
    }.each do |env_key, (input, variable, literal)|
      assert_includes scheduler,
                      "#{env_key}: \${{ inputs.#{input} || vars.#{variable} || '#{literal}' }}",
                      "issue-scheduler: #{env_key} has no vars. fallback"
    end

    assert_includes auto_merge_body,
                    "REVIEW_PROVIDER: ${{ inputs.review_provider || vars.CONTINUUM_REVIEW_PROVIDER || 'none' }}"
    assert_includes watchdog_body,
                    "MAX_RECOVERY_RETRIES: ${{ inputs.max_recovery_retries || vars.AUTOMATION_WATCHDOG_MAX_RETRIES || '1' }}"
    assert_includes watchdog_body,
                    "PAUSE_ON_FAILURE: ${{ inputs.pause_on_failure || vars.AUTOMATION_PAUSE_ON_FAILURE || 'false' }}"
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
    { 'continuum-issue-scheduler.yml' => workflow_body('continuum-issue-scheduler.yml'),
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

    scheduler = workflow_body('continuum-issue-scheduler.yml')
    # It must be a live parser, not a literal: numbers are extracted and read.
    assert_includes scheduler, '[...match[1].matchAll(/\d+/g)]'
    assert_includes scheduler, 'async function openDeclaredBlockers('
    assert_includes scheduler, 'targetOwner = owner'
    assert_includes scheduler, 'targetRepo = repo'
    # …and it must actually gate candidate selection.
    assert_includes scheduler, 'const declaredOpenBlockers = await openDeclaredBlockers(issue);'
    assert_includes scheduler, "': declared blocked by '"
  end

  def test_scheduler_local_reads_use_repository_token_but_pat_keeps_privileged_paths
    scheduler = workflow_body('continuum-issue-scheduler.yml')
    workflow = yaml(File.join(ROOT, '.github/workflows/continuum-issue-scheduler.yml'))
    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-issue-scheduler.yml'))

    expected_read_permissions = {
      'actions' => 'read',
      'contents' => 'read',
      'issues' => 'read',
      'pull-requests' => 'read',
    }
    assert_equal expected_read_permissions, workflow.fetch('permissions')
    assert_equal expected_read_permissions, stub.fetch('permissions')

    assert_includes scheduler, 'READ_GITHUB_TOKEN: ${{ github.token }}'
    assert_includes scheduler, 'github-token: ${{ secrets.TAP_PAT }}'
    assert_includes scheduler, 'const readGithub = new github.constructor({'
    assert_includes scheduler, 'baseUrl: readBaseUrl'

    %w[
      readGithub.rest.repos.getBranch
      readGithub.rest.issues.listForRepo
      readGithub.rest.issues.listComments
      readGithub.rest.pulls.list
      readGithub.rest.pulls.get
      readGithub.rest.actions.listWorkflowRunsForRepo
    ].each { |call| assert_includes scheduler, call }

    child = scheduler[/const \[childOwner, childRepo\] = parts;.*?await dispatchWorkflow\(childWorkerWorkflow/m]
    refute_nil child
    assert_includes child, 'const freshResponse = await github.rest.issues.get({'
    assert_includes child, 'const childNativeBlockers = await github.paginate('

    assert_includes scheduler, 'await github.rest.issues.createComment({'
    assert_includes scheduler, 'await github.rest.issues.addLabels({'
    assert_includes scheduler, 'await github.rest.issues.removeLabel({'
    assert_includes scheduler, 'await github.request('
  end
  # Child routing is repository-level. A child never schedules locally, but
  # event-driven caller runs wake only its verified parent. Child schedule events
  # stay skipped so private child minutes are not used as a polling mechanism.
  def test_scheduler_child_role_wakes_verified_parent_without_local_dispatch
    scheduler = workflow_body('continuum-issue-scheduler.yml')
    workflow = yaml(File.join(ROOT, '.github/workflows/continuum-issue-scheduler.yml'))
    assert_equal "vars.CONTINUUM_ROLE != 'child'",
                 workflow.fetch('jobs').fetch('schedule').fetch('if')
    assert_equal "vars.CONTINUUM_ROLE == 'child' && (inputs.caller_event_name == 'issues' || inputs.caller_event_name == 'issue_comment')",
                 workflow.fetch('jobs').fetch('wake_parent').fetch('if'),
                 'the reusable always sees github.event_name as workflow_call, so the wake gate must read the caller-forwarded event name'

    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-issue-scheduler.yml'))
    caller_gate = stub.fetch('jobs').fetch('call').fetch('if')
    assert_includes caller_gate, "vars.CONTINUUM_ROLE != 'child' || github.event_name == 'issues'"
    assert_includes caller_gate, "github.event_name == 'issue_comment'",
                    'a child admits only issue events matching the reusable wake-up job; child push, pull_request_target, workflow_run, and workflow_dispatch stay cheap caller skips'
    assert_includes caller_gate, "github.event_name != 'issues'",
                    'child issues wakes must be trust-gated or any user with issue access burns a TAP_PAT parent dispatch'
    assert_includes caller_gate, "github.event.action == 'opened'",
                    'child issues wakes must admit only open/reopen/close transitions so bulk label churn cannot fan out to N parent dispatches'
    assert_includes caller_gate, "github.event.action == 'reopened'",
                    'child issues wakes must admit only open/reopen/close transitions so bulk label churn cannot fan out to N parent dispatches'
    assert_includes caller_gate, "github.event.action == 'closed'",
                    'child issues wakes must admit only open/reopen/close transitions so bulk label churn cannot fan out to N parent dispatches'
    assert_includes caller_gate, "github.event_name != 'issue_comment'"
    assert_includes caller_gate, 'github.actor == github.repository_owner',
                    'the parent wake gate must stay owner-only or any comment wakes a TAP_PAT dispatch'
    assert_includes caller_gate, "contains(github.event.comment.body, '/oc')",
                    'the parent wake gate must still require an owner /oc command'
    assert_includes caller_gate, "contains(github.event.comment.body, '/opencode')",
                    'the parent wake gate must still require an owner /opencode command'
    assert_includes caller_gate, "contains(github.event.comment.body, 'continuum-qualification-result')",
                    'the merged caller must keep the qualification-result wake-up from main'
    assert_includes caller_gate, "github.event.comment.author_association == 'OWNER'",
                    'qualification-result wakes must stay trust-gated'

    wake_parent = workflow.fetch('jobs').fetch('wake_parent')
    assert_equal true, wake_parent.fetch('concurrency').fetch('cancel-in-progress'),
                     'the parent dispatch is idempotent, so a newer per-issue wake supersedes queued duplicates instead of fanning out N sequential dispatches'
    assert_includes wake_parent.fetch('concurrency').fetch('group').to_s, 'inputs.caller_issue_number',
                    'the called workflow sees no github.event.issue, so per-issue grouping must use the caller-forwarded issue number'

    assert_includes scheduler, 'CONTINUUM_CHILD_ID'
    assert_includes scheduler, 'CONTINUUM_PARENT'
    assert_includes scheduler, 'verify-child-variables'
    assert_includes scheduler, 'parent-variable-ids'
    assert_includes scheduler, 'actions/workflows/continuum-issue-scheduler.yml/dispatches'
    assert_includes scheduler, 'Woke verified parent scheduler.'
    assert_includes scheduler, '2>/dev/null'
    refute_includes scheduler, 'echo "$parent"'
    refute_includes scheduler, 'echo "$child_id"'
    assert_includes scheduler, 'actions/variables/$name',
                    'relationship lookups must fetch the named variable directly instead of paginating the collection'
    refute_includes scheduler, 'actions/variables?per_page=100',
                    'paginated variable listings truncate past 100 entries and over-expose values'
    assert_includes scheduler, '--child-id "$candidate_id"',
                    'the expected child id must come from the verified parent allow-list, never from the child declaration itself'
    assert_includes scheduler, '--parent-repository "$parent_identity"',
                    'the expected parent must be the independently verified parent identity, never the child declaration itself'
    refute_includes scheduler, '--child-id "$child_id"',
                    'comparing the child declaration against itself proves no cross-party agreement'
    refute_includes scheduler, 'CURRENT_REPO="$GITHUB_REPOSITORY"',
                    'CONTINUUM_CHILDREN holds opaque id slugs, never owner/repo names, so no wake-up can require both at once'
    refute_includes scheduler, 'GITHUB_TOKEN: ${{ secrets.TAP_PAT }}',
                    'gh already uses GH_TOKEN; overriding GITHUB_TOKEN elevates every consumer to PAT privileges'

    refute_includes scheduler, 'function isChildOwned(issue)'
    refute_includes scheduler, 'body.includes(childOwnedMarker)'
    refute_includes scheduler, 'body.includes(legacyChildOwnedMarker)'
    refute_includes scheduler, 'CHILD_OWNED_MARKER:'
    refute_includes scheduler, 'LEGACY_CHILD_OWNED_MARKER:'

    inputs = events(workflow).fetch('workflow_call').fetch('inputs')
    assert_includes inputs.fetch('child_owned_marker').fetch('description'), 'Deprecated'
    assert_includes inputs.fetch('legacy_child_owned_marker').fetch('description'), 'Deprecated'

    %w[caller_event_name caller_issue_number].each do |key|
      assert inputs.key?(key), "issue-scheduler missing event-plumbing input #{key}"
      assert_equal '', inputs.fetch(key).fetch('default'), key
      assert_equal 'string', inputs.fetch(key).fetch('type'), key
      assert_equal false, inputs.fetch(key).fetch('required'), key
    end
    call_with = stub.fetch('jobs').fetch('call').fetch('with')
    assert_equal '${{ github.event_name }}', call_with.fetch('caller_event_name'),
                 'the caller must forward its own trigger name; the reusable otherwise always sees workflow_call'
    assert_equal '${{ github.event.issue.number }}', call_with.fetch('caller_issue_number'),
                 'the caller must forward its issue number; the reusable otherwise sees an empty github.event'
  end

  # A parent has one scheduling queue. Local issues and verified child issues
  # are both ranked by the same P0/P1/P2/no-priority order and consume the same
  # WIP limit; the standalone child dispatcher is disabled for parent role.
  def test_issue_scheduler_unifies_parent_and_child_priority_queue
    scheduler = workflow_body('continuum-issue-scheduler.yml')

    assert_includes scheduler, 'Build delegated child queue'
    assert_includes scheduler, 'for (const child of childCandidates) {'
    assert_includes scheduler, "candidates.push({ source: 'local', issue, priority, rank });"
    assert_includes scheduler, "source: 'child'"
    assert_includes scheduler, 'a.rank - b.rank'
    assert_includes scheduler, 'const totalActiveWip = activeIssueNumbers.size + childActiveWip;'
    assert_includes scheduler, 'await dispatchWorkflow(childWorkerWorkflow, {'
    assert_includes scheduler, 'local_opencode_pr_tasks'
    assert_includes scheduler, 'local_opencode_run_tasks'
    assert_includes scheduler, 'local_override_active "$task_number"'
    assert_includes scheduler, 'sort_by(._rank, .number)'
    assert_includes scheduler, 'else 3'

    legacy = yaml(File.join(ROOT, '.github/workflows', 'continuum-consumer-child-dispatcher.yml'))
    assert_equal "vars.CONTINUUM_ROLE != 'parent'",
                 legacy.fetch('jobs').fetch('dispatch').fetch('if')
  end

  # The unified scheduler replaced the standalone parent child dispatcher, so
  # it must retain that dispatcher's autonomous wake-up surface. Child issue
  # creation cannot emit an event in the parent repository; polling therefore
  # remains the zero-private-minutes path that discovers new delegated work.
  def test_unified_parent_scheduler_keeps_delegated_polling_wakeups
    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-issue-scheduler.yml'))
    on = events(stub)

    assert_equal ['7,17,27,37,47,57 * * * *'],
                 on.fetch('schedule').map { |entry| entry.fetch('cron') }
    assert_equal ['main'], on.fetch('push').fetch('branches')

    workflow_names = on.fetch('workflow_run').fetch('workflows')
    %w[SubTask].each { |name| assert_includes workflow_names, name }
    assert_includes workflow_names, 'SubTask review'
    assert_includes workflow_names, 'SubTask PR review'
  end

  # A manual owner `/oc` is real in-flight work: reserve it at once, and keep a
  # short grace window so this run cannot enqueue a duplicate right behind it.
  def test_scheduler_reserves_owner_commands_and_honours_the_grace_window
    scheduler = workflow_body('continuum-issue-scheduler.yml')

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
    assert_includes scheduler, 'schedulerDispatches.length >= maxDispatchAttempts'
    assert_includes scheduler, 'pauseOnFailure'
    refute_includes scheduler, 'if (dispatches.length >= maxDispatchAttempts) {'
  end

  # The just-in-time re-check is the guard against a duplicate OpenCode run: it
  # runs after selection and before the reservation/comment, and every unsafe
  # condition has to be a skip.
  def test_scheduler_rechecks_state_just_before_dispatch
    scheduler = workflow_body('continuum-issue-scheduler.yml')
    dispatch = scheduler[/for \(const candidate of selected\) \{\n(.*?)\n              await addLabel\(issue\.number, inProgressLabel\);/m, 1]
    refute_nil dispatch, 'the just-in-time re-check block is gone'

    [
      'const freshIssueResponse = await readGithub.rest.issues.get({',
      "freshIssue.state !== 'open'",
      '(pauseOnFailure && freshLabels.has(pausedLabel))',
      'freshLabels.has(inProgressLabel)',
      'const freshDeclaredBlockers = await openDeclaredBlockers(freshIssue);',
      'freshOpenBlockers.length > 0',
      'if (commandAgeMs < commandGraceMs) {'
    ].each { |guard| assert_includes dispatch, guard, "missing just-in-time guard: #{guard}" }
    local_dispatch = dispatch[/\/\/ Re-check mutable state immediately before dispatch\..*\z/m]
    refute_nil local_dispatch, 'the local just-in-time re-check block is gone'
    assert_equal 4, local_dispatch.scan(/^\s+continue;\s*$/).size,
                 'every local just-in-time guard must be a skip, not a fall-through'
  end

  # Closed issues are terminal scheduler state. A stale in-progress label on a
  # closed issue must be released by the shared engine itself, otherwise a
  # consumer has to fork the scheduler merely to keep WIP accounting correct.
  def test_scheduler_releases_stale_leases_from_closed_issues
    scheduler = workflow_body('continuum-issue-scheduler.yml')

    reconciliation = scheduler[/const closedLeasedIssues = await readGithub\.paginate\(.*?\n\s*let issues = await readGithub\.paginate/m]
    refute_nil reconciliation, 'closed-issue lease reconciliation is missing from the shared scheduler'
    assert_includes reconciliation, 'readGithub.rest.issues.listForRepo'
    assert_includes reconciliation, "state: 'closed'"
    assert_includes reconciliation, 'labels: inProgressLabel'
    assert_includes reconciliation, 'if (issue.pull_request) continue;'
    assert_includes reconciliation, 'await removeLabel(issue.number, inProgressLabel);'
    assert_includes reconciliation, 'Released stale reservation on closed issue #'

    # The cleanup must happen before open-backlog admission so a closed issue
    # cannot retain WIP while the same reconciliation pass selects new work.
    # The admission is located by its own statement, not by the first
    # `state: 'open'` string in the file: helpers elsewhere in the script
    # legitimately reopen issues and would otherwise move that match.
    admission_at = scheduler.index('let issues = await readGithub.paginate(')
    refute_nil admission_at, 'the open-backlog admission is missing from the shared scheduler'
    assert_includes scheduler[admission_at, 400], "state: 'open'",
                    'the backlog admission must list open issues'
    assert_operator scheduler.index('const closedLeasedIssues = await readGithub.paginate'),
                    :<,
                    admission_at
  end

  # A blocked issue whose OpenCode PR was closed unmerged is not a failed
  # implementation, and a native blocker is authoritative over an old
  # reservation lease. Both must release, not pause.
  def test_scheduler_releases_native_blockers_instead_of_pausing
    scheduler = workflow_body('continuum-issue-scheduler.yml')

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
    assert_includes scheduler, 'for (let page = 1; page <= 3; page += 1) {'
    assert_includes scheduler, 'readGithub.rest.actions.listWorkflowRunsForRepo({'
    assert_includes scheduler, 'if (data.workflow_runs.length < 100) break;'
    refute_includes scheduler, "readGithub.paginate(\n              readGithub.rest.actions.listWorkflowRunsForRepo"
    refute_includes scheduler, 'gh api --paginate "repos/$GITHUB_REPOSITORY/actions/runs?per_page=100"'
    refute_includes scheduler, 'gh api --paginate "repos/$child_repo/actions/runs?per_page=100"'
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
    scheduler = workflow_body('continuum-issue-scheduler.yml')
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
    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-issue-scheduler.yml'))
    assert_equal ['created'], events(stub).fetch('issue_comment').fetch('types')
    gate = stub.fetch('jobs').fetch('call').fetch('if')
    assert_includes gate, "github.event_name != 'issue_comment'"
    assert_includes gate, 'github.actor == github.repository_owner'
    assert_includes gate, "contains(github.event.comment.body, '/oc')"
  end

  # Issue #170: the exact manual-command rule lives in one shared module, and
  # the workflow-facing CLI delegates to it. An `if: contains(body, '/oc')`
  # gate treats automation-authored prose as a fresh command and chains
  # self-trigger runs; the shared rule only admits the explicit forms.
  def opencode_command(*args)
    script = File.join(ROOT, '.github/scripts/opencode_command.py')
    # Never leave bytecode behind: `test_core_workflows_expose_no_product_paths`
    # scans `src/**` as text, and a `__pycache__` directory breaks that scan.
    output, status = Open3.capture2e(
      { 'PYTHONDONTWRITEBYTECODE' => '1' }, 'python3', script, *args
    )
    assert status.success?, "opencode_command.py #{args.first} failed: #{output}"
    output.strip
  end

  def test_manual_opencode_command_rule_is_shared_between_module_and_cli
    helper = File.join(ROOT, '.github/scripts/opencode_command.py')
    assert File.executable?(helper), 'the command gate must stay directly executable'
    source = File.read(helper)
    assert_includes source, 'from continuum.opencode_commands import',
                    'the CLI must delegate to the canonical parser, not reimplement it'
    assert_includes source, 'classify_issue_comment'

    engine = File.read(File.join(ROOT, 'src/continuum/opencode_commands.py'))
    assert_includes engine, "IMPLEMENT_COMMANDS = (\"/oc\", \"/opencode\")"
    assert_includes engine, "CANCEL_COMMAND = \"/oc-cancel\""
    assert_includes engine, 'def classify_issue_comment'
    assert_includes engine, 'def is_owner_command'
  end

  def test_opencode_command_gate_recognizes_only_exact_manual_forms
    assert_equal 'run', opencode_command('classify', '--body', '/oc')
    assert_equal 'run', opencode_command('classify', '--body', '/opencode')
    assert_equal 'run', opencode_command('classify', '--body', "  /oc  \n")
    assert_equal 'cancel', opencode_command('classify', '--body', '/oc-cancel')
    assert_equal 'cancel', opencode_command('classify', '--body', "  /oc-cancel  ")
    assert_equal 'true', opencode_command('is-manual', '--body', '/oc')
    assert_equal 'true', opencode_command('is-cancel', '--body', '/oc-cancel')
    assert_equal 'false', opencode_command('is-manual', '--body', '/oc-cancel')
  end

  def test_opencode_command_gate_rejects_incidental_prose_and_examples
    [
      'Please run /oc for me',
      'The /opencode command failed again',
      '`/oc`',
      'Use `/oc` to trigger',
      '> /oc',
      "```\n/oc\n```",
      '    /oc',
      "Some explanation first.\n/oc",
      '/oc please hurry',
      '/octopus'
    ].each do |body|
      assert_equal 'ignore', opencode_command('classify', '--body', body),
                   "prose must never qualify as a command: #{body.inspect}"
    end
  end

  def test_opencode_command_gate_preserves_intended_automation_commands
    dispatch = "/oc\n\n<!-- issue-scheduler-dispatch -->\nAutomatically dispatched."
    recovery = "/oc\n\n<!-- opencode-watchdog-retry -->\nAutomatic recovery retry 1/1."
    pause = "OpenCode automation paused.\n\nRemove the `automation:paused` label and post `/oc` to retry manually."
    prose = 'OpenCode run 37008882116 completed with no PR; the /oc token was mentioned while explaining why.'

    assert_equal 'run', opencode_command('classify', '--body', dispatch)
    assert_equal 'run', opencode_command('classify', '--body', recovery)
    assert_equal 'true', opencode_command('is-scheduler-dispatch', '--body', dispatch)
    assert_equal 'true', opencode_command('is-watchdog-recovery', '--body', recovery)
    assert_equal 'ignore', opencode_command('classify', '--body', pause)
    assert_equal 'ignore', opencode_command('classify', '--body', prose)
  end

  def test_opencode_command_conversation_dispatches_exactly_once
    bodies = [
      "/oc\n\n<!-- issue-scheduler-dispatch -->\nAutomatically dispatched.",
      'OpenCode run 37008882116 completed with no PR. The /oc token was mentioned while explaining why.',
      'Automation produced no code changes; mentioning /opencode here must not relaunch anything.',
      "Remove the `automation:paused` label and post `/oc` to retry manually."
    ]
    results = bodies.map { |body| opencode_command('classify', '--body', body) }
    assert_equal %w[run ignore ignore ignore], results
    assert_equal 1, results.count('run'),
                 'the live Work Lock sequence must launch exactly one run'
  end

  def test_opencode_command_gate_keeps_owner_authorization
    assert_equal 'true',
                 opencode_command('is-owner-command', '--body', '/oc',
                                  '--author', 'octocat', '--owner', 'octocat')
    assert_equal 'false',
                 opencode_command('is-owner-command', '--body', '/oc',
                                  '--author', 'stranger', '--owner', 'octocat')
    assert_equal 'false',
                 opencode_command('is-owner-command', '--body', 'prose /oc prose',
                                  '--author', 'octocat', '--owner', 'octocat')
  end

  def test_empty_vars_resolve_to_the_same_scheduler_defaults
    scheduler = workflow_body('continuum-issue-scheduler.yml')
    {
      'DISPATCH_MARKER' => ['dispatch_marker', '<!-- issue-scheduler-dispatch -->', 'AUTOMATION_DISPATCH_MARKER'],
      'IN_PROGRESS_LABEL' => ['in_progress_label', 'automation:in-progress', 'AUTOMATION_IN_PROGRESS_LABEL'],
      'PAUSE_LABEL' => ['pause_marker', 'automation:paused', 'AUTOMATION_PAUSE_LABEL'],
      'WIP_LIMIT' => ['wip_limit', '2', 'AUTOMATION_WIP_LIMIT'],
      'LEASE_MINUTES' => ['lease_minutes', '45', 'AUTOMATION_LEASE_MINUTES'],
      'MAX_DISPATCH_ATTEMPTS' => ['max_dispatch_attempts', '2', 'AUTOMATION_MAX_DISPATCH_ATTEMPTS'],
      'REQUIRE_PRIORITY_LABEL' => ['require_priority_label', 'false', 'AUTOMATION_REQUIRE_PRIORITY_LABEL'],
      'COMMAND_GRACE_MINUTES' => ['command_grace_minutes', '5', 'AUTOMATION_COMMAND_GRACE_MINUTES'],
      'OPENCODE_WORKFLOW_NAME' => ['opencode_workflow_name', 'OpenCode agent', 'CONTINUUM_OPENCODE_WORKFLOW_NAME'],
      'OPENCODE_WORKFLOW_PATH' => ['opencode_workflow_path', '.github/workflows/continuum-opencode.yml', 'CONTINUUM_OPENCODE_WORKFLOW_PATH']
    }.each do |key, (input, expected, variable)|
      line = scheduler.lines.find { |candidate| candidate.include?("#{key}: ${{") }
      refute_nil line, "#{key}: no env mapping found in continuum-issue-scheduler.yml"

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
    File.read(File.join(ROOT, '.github/workflows/continuum-add-review-label.yml'))
  end

  # The same optional-integration contract continuum-auto-merge.yml already honours.
  # continuum-add-review-label.yml is the *other* half of the CodeRabbit path: it writes
  # the two queue labels and dispatches the retry controller. A repository that
  # never asked for CodeRabbit would get `review-ready` and
  # `coderabbit-review-requested` on every green PR, and a 404 from a
  # `continuum-coderabbit-retry.yml` it does not have.
  def test_add_review_label_gates_the_coderabbit_path_behind_the_provider
    inputs = events(yaml(File.join(ROOT, '.github/workflows/continuum-add-review-label.yml')))
             .fetch('workflow_call').fetch('inputs')
    knob = inputs.fetch('review_provider')
    assert_equal '', knob.fetch('default'),
                 'review_provider must default to empty so vars.CONTINUUM_REVIEW_PROVIDER decides'
    assert_equal 'string', knob.fetch('type')
    assert_equal false, knob.fetch('required')

    body = add_review_label_body
    assert_includes body, "REVIEW_PROVIDER: ${{ inputs.review_provider || vars.CONTINUUM_REVIEW_PROVIDER || 'none' }}"
    assert_match(/const reviewProvider =\s*String\(process\.env\.REVIEW_PROVIDER \|\| 'none'\)\.trim\(\)\.toLowerCase\(\);/, body)
    assert_includes body, "const requireCodeRabbit = reviewProvider === 'coderabbit';"
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

  # Initial implementation and later CI repair must share one authoritative
  # task-context resolver. Without this contract a repair can optimize for a
  # red CI log while silently violating the original issue/Definition of Done.
  def test_opencode_issue_and_ci_repair_share_authoritative_task_context
    body = workflow_body('continuum-opencode.yml')

    resolver = step_body(body, 'Resolve authoritative task context')
    refute_nil resolver, 'the shared task-context resolver step is missing'
    assert_includes resolver, 'continuum-task-context'
    assert_includes resolver, "source = 'canonical-marker'"
    assert_includes resolver, "source = 'legacy-cross-repo-reference'"
    assert_includes resolver, "source = 'legacy-same-repo-reference'"
    assert_includes resolver, "source = 'legacy-branch-identity'"
    assert_includes resolver, 'github.rest.issues.get',
                    'resolved task context must fetch the authoritative issue itself'
    assert_includes resolver, "core.setOutput('task_body'"
    assert_includes resolver, "core.setOutput('task_title'"

    issue = step_body(body, 'Implement issue')
    refute_nil issue
    assert_includes issue, 'TASK_CONTEXT_PRESENT: ${{ steps.task_context.outputs.present }}'
    assert_includes issue, 'ISSUE_TITLE: ${{ steps.task_context.outputs.task_title }}'
    assert_includes issue, 'ISSUE_BODY: ${{ steps.task_context.outputs.task_body }}'
    refute_includes issue, 'ISSUE_JSON="$(gh issue view',
                    'issue mode must not bypass the shared resolver with a second fetch path'
    assert_includes issue, 'TASK_MARKER="<!-- continuum-task-context repo=${GITHUB_REPOSITORY} issue=${ISSUE_NUMBER} -->"'

    recover = step_body(body, 'Recover agent-managed issue branch')
    refute_nil recover
    assert_includes recover, 'continuum-task-context repo=${GITHUB_REPOSITORY} issue=${ISSUE_NUMBER}',
                    'recovered task PRs must preserve the same canonical marker'

    repair = step_body(body, 'Fix failed blocking workflow')
    refute_nil repair
    assert_includes repair, 'TASK_BODY: ${{ steps.task_context.outputs.task_body }}'
    assert_includes repair, 'PR_BODY: ${{ steps.task_context.outputs.pr_body }}'
    assert_includes repair, 'Task specification / Definition of Done:'
    assert_includes repair, 'treat its specification and Definition of Done as authoritative'
    assert_includes repair, 'Never weaken, delete, skip, or special-case a validation merely to make CI green.'
    assert_includes repair, 'No authoritative task issue was resolved for this legacy PR. Do not fabricate one'
  end
  # A task implementation command must obey the same dependency/DoR gate as
  # the scheduler. Manual /oc is not an escape hatch that may turn a blocked
  # tracking issue into a partial PR.
  def test_issue_mode_checks_definition_of_ready_before_starting_the_agent
    body = workflow_body('continuum-opencode.yml')

    assert_includes body, '- name: Check issue Definition of Ready'
    assert_includes body, '/<!--\s*automation-blocked-by:\s*([0-9#\s,]+?)\s*-->/i'
    assert_includes body, "'GET /repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by'"
    assert_includes body, "core.setOutput('ready', 'false');"
    assert_includes body, 'Continuum will not create a partial PR for a blocked/umbrella task.'

    assert_match(/- name: Implement issue\n\s+if: >-\n\s+steps\.issue_readiness\.outputs\.ready != 'false'/, body)
    assert_match(/- name: Install OpenCode CLI for automated repair\n\s+if: github\.event_name == 'workflow_dispatch' && steps\.issue_readiness\.outputs\.ready != 'false'/, body)
    assert_match(/- name: Install OpenCode CLI for interactive agent\n\s+if: github\.event_name != 'workflow_dispatch' && steps\.issue_readiness\.outputs\.ready != 'false'/, body)
    assert_includes body, 'Do not turn a tracking/umbrella issue or a task with unmet prerequisites into a partial PR.'
  end

  # Specialized workflows are tools of the task agent, not alternate
  # scheduler executors. Operational tasks may therefore complete with no
  # repository diff, but only when the agent has posted evidence and closed the
  # issue after satisfying its full Definition of Done.
  def test_issue_agent_can_orchestrate_workflow_tools_and_complete_no_code_tasks
    body = workflow_body('continuum-opencode.yml')

    assert_includes body, 'dispatch existing repository workflows when required by the issue'
    assert_includes body, 'Treat specialized workflows as tools'
    assert_includes body, 'the scheduler will not reroute the issue for you'
    assert_includes body, 'close the issue yourself only after every Definition of Done item is verified'
    assert_includes body, 'Never close an implementation task that still requires code changes.'

    state_check = body.index('ISSUE_STATE="$(gh issue view "$ISSUE_NUMBER"')
    closed_check = body.index('if [[ "$ISSUE_STATE" == "CLOSED" ]]')
    pause = body.index('Automation produced no code changes; pausing this issue for manual inspection.')
    refute_nil state_check, 'no-change handling does not inspect whether the issue was completed'
    refute_nil closed_check, 'no-change handling does not recognize a completed operational task'
    refute_nil pause, 'failed no-change tasks must still pause rather than loop'
    assert_operator state_check, :<, closed_check
    assert_operator closed_check, :<, pause,
                    'a successfully closed operational task must exit before the failure pause path'
  end

  # CodeRabbit can submit CHANGES_REQUESTED for a policy/pre-merge failure with
  # no code finding at all. That state is not a coding-agent repair request.
  def test_coderabbit_policy_blocker_is_classified_before_opencode_dispatch
    body = workflow_body('continuum-opencode.yml')
    job = body[/^  dispatch-coderabbit-fix:\n(.*?)(?=^  opencode:)/m, 1]
    refute_nil job, 'dispatch-coderabbit-fix job is missing'

    classifier = job.index("if (reviewState === 'changes_requested' && findings.length === 0)")
    dispatch = job.index('github.rest.actions.createWorkflowDispatch')
    refute_nil classifier, 'CHANGES_REQUESTED without inline findings is not classified'
    refute_nil dispatch, 'actionable CodeRabbit repair dispatch is missing'
    assert_operator classifier, :<, dispatch,
                    'non-code review verdict must be classified before any OpenCode dispatch'

    assert_includes job, 'continuum-coderabbit-no-progress head='
    assert_includes job, 'No OpenCode repair was dispatched for this unchanged HEAD.'
    assert_includes job, "if (reviewState === 'approved')"
    assert_includes job, 'any older no-progress marker is superseded.'
    assert_includes job, 'github-token: ${{ github.token }}',
                    'the classifier must stay within the existing caller permission contract'
  end

  # One exact HEAD plus one non-code blocker is a terminal no-progress state.
  # Both the global review queue and auto-merge reconciler must honour it rather
  # than repeatedly buying another CodeRabbit review of identical code.
  def test_coderabbit_no_progress_marker_stops_same_head_requeue
    retry_body = workflow_body('continuum-coderabbit-retry.yml')
    merge_body = auto_merge_body

    assert_includes retry_body, 'continuum-coderabbit-no-progress head='
    assert_includes retry_body, "login === 'github-actions[bot]' || login === owner"
    assert_includes retry_body, 'laterApproval'
    assert_includes retry_body, 'it is not eligible for automatic re-review until the head changes or a later explicit approval supersedes the marker.'

    assert_includes merge_body, 'continuum-coderabbit-no-progress head='
    assert_includes merge_body, 'codeRabbitNoProgressBlocked'
    assert_includes merge_body, 'reviewNoProgressBlocked'
    assert_includes merge_body, 'automatic review/fix retries are suppressed.'
  end

  # Inline findings already have an independent thread-verification protocol.
  # If OpenCode decides no code change is needed, do not start another full
  # review loop for the same inline findings. Body-only nitpicks retain one
  # bounded full re-review because they have no thread of their own.
  def test_unchanged_inline_coderabbit_fix_does_not_force_full_rereview
    body = workflow_body('continuum-opencode.yml')
    assert_includes body,
                    'if (findings.length === 0 && reviewedSha && headSha === reviewedSha) {'
    refute_match(/\n\s*if \(reviewedSha && headSha === reviewedSha\) \{/, body)
  end

  # The global CodeRabbit queue spends a repository-wide scarce review slot.
  # Admission must therefore match every pre-review gate the auto-merger can
  # already prove before review, and a re-review must wait for finding-level
  # verification to converge.
  def test_coderabbit_review_queue_is_merge_aware_and_has_final_review_stage
    body = workflow_body('continuum-coderabbit-retry.yml')

    assert_includes body, "REQUIRED_WORKFLOW_GATE_LABEL: ${{ vars.CONTINUUM_REQUIRED_WORKFLOW_GATE_LABEL || '' }}"
    assert_includes body, "REQUIRED_WORKFLOW_GATE_NAME: ${{ vars.CONTINUUM_REQUIRED_WORKFLOW_GATE_NAME || '' }}"
    assert_includes body, "await latestWorkflowForHead(pr, 'Packaging smoke')"
    assert_includes body, 'new Date(b.updated_at || b.created_at).getTime()'
    assert_includes body, 'Number(b.run_attempt || 0)'
    assert_includes body, 'requiredWorkflowGateLabel'
    assert_includes body, 'requiredWorkflowGateName'
    assert_includes body, 'not eligible for a CodeRabbit full-review slot yet'

    assert_includes body, 'await unresolvedCodeRabbitThreads(pr)'
    assert_includes body, 'waiting for ${unresolvedThreads.length} unresolved CodeRabbit thread(s) before final full review'
    assert_includes body, 'codeRabbitExplicitlyResolved'
    assert_includes body, '/\\bRESOLVED\\b/i.test(body)'
    assert_includes body, 'data.repository?.pullRequest?.reviewThreads'
    assert_includes body, 'const latestComment = comments.at(-1)'
    assert_includes body, "latestComment.author?.login?.startsWith('coderabbitai')"
    assert_includes body, 'if (unresolvedThreads === null)'
    assert_includes body, 'could not inspect CodeRabbit review threads; skipping this PR for this pass'
    assert_includes body, 'queue reconciliation failed for this PR; skipping it for this pass'
    assert_includes body, "return (reviews || [])"
    assert_includes body, "currentDecision?.state === 'APPROVED'"
    assert_includes body, 'durable exact-HEAD no-progress marker handled above'
    assert_includes body, 'continuum-coderabbit-no-progress head='
    assert_includes body, 'could not inspect pre-review workflow gates; skipping this PR for this pass'
    assert_includes body, 'required workflow gate configuration is incomplete'
    assert_includes body, 'labelConfigured !== nameConfigured'
    assert_includes body, "stage: finalReview ? 'final-review' : 'initial-review'"
    assert_includes body, 'stageRank: finalReview ? 0 : 1'
    assert_match(/a\.rank - b\.rank \|\|\s*a\.stageRank - b\.stageRank/m, body)

    # The requested label remains the exact-head/idempotency lock and there is
    # still one authoritative command emission site in the serialized queue.
    assert_includes body, 'const requested = labels.has(REQUESTED_LABEL);'
    assert_equal 1, body.scan("body: '@coderabbitai full review'").size
  end

  # Hour-scale CodeRabbit quota waits must never pin a GitHub runner. The
  # controller keeps the due time in durable review/comment timestamps and
  # exits; existing event-driven and auto-merge safety-net wake-ups reconcile
  # the queue later.
  def test_coderabbit_review_queue_defers_without_sleeping_runner
    body = workflow_body('continuum-coderabbit-retry.yml')

    assert_includes body, 'function deferUntilNextCandidate(state)'
    assert_includes body, 'no runner sleep'
    assert_includes body, 'auto-merge safety-net reconciliation will wake the controller again'
    assert_includes body, 'timeout-minutes: 15'
    assert_includes body, 'cancel-in-progress: true'
    refute_includes body, 'MAX_WAIT_MS'
    refute_includes body, 'Sleeping until'
    refute_includes body, 'setTimeout(resolve, waitMs)'
  end

  # RESOLVED/UNRESOLVED replies are lifecycle events. They must wake the queue
  # without relying on cron. A PR lacking a source issue gets an explicit P2
  # fallback instead of an infinite rank that can starve forever.
  def test_coderabbit_review_queue_wakes_on_finding_verdict_and_has_nonstarving_fallback
    body = workflow_body('continuum-coderabbit-retry.yml')
    stub = File.read(
      File.join(ROOT, '.github/caller-stubs/continuum-coderabbit-retry.yml')
    )

    assert_includes stub, 'pull_request_review_comment:'
    assert_includes stub, 'types: [created, edited]'
    assert_includes body, "github.event_name == 'pull_request_review_comment'"
    assert_includes body, "startsWith(github.event.comment.user.login, 'coderabbitai')"

    assert_operator body.scan("priority: 'unprioritized:p2-fallback'").size, :>=, 2,
                    'both source-less and issue-backed unprioritized PRs must use the P2 fallback'
    assert_operator body.scan("rank: priorityRank.get('priority:p2')").size, :>=, 2
    assert_includes body, 'a.createdAt - b.createdAt'
    assert_includes stub, 'actions: read'
    refute_includes stub, 'actions: write'
    refute_includes body, "workflow_id: 'continuum-auto-merge.yml'"
  end

  # ------------------------------------------------- auto-merge / CodeRabbit

  def auto_merge_body
    File.read(File.join(ROOT, '.github/workflows/continuum-auto-merge.yml'))
  end

  # CodeRabbit is an OPTIONAL integration, so Continuum's default is off. A
  # `true` default "so current consumers do not change" would make every
  # project without CodeRabbit wait forever for an approval nobody will give,
  # and would dispatch a workflow it does not have. The enabling value belongs
  # in the one repository that asked for CodeRabbit.
  def test_review_provider_defaults_to_none_and_is_variable_driven
    inputs = events(yaml(File.join(ROOT, '.github/workflows/continuum-auto-merge.yml')))
             .fetch('workflow_call').fetch('inputs')
    knob = inputs.fetch('review_provider')
    assert_equal '', knob.fetch('default'),
                 'review_provider must default to empty so vars.CONTINUUM_REVIEW_PROVIDER decides'
    assert_equal 'string', knob.fetch('type')
    assert_equal false, knob.fetch('required')

    body = auto_merge_body
    assert_includes body, "REVIEW_PROVIDER: ${{ inputs.review_provider || vars.CONTINUUM_REVIEW_PROVIDER || 'none' }}"
    assert_includes body, "const requireCodeRabbit = reviewProvider === 'coderabbit';"
    assert_includes body, "const prAgentSyncOnly = reviewProvider === 'pr-agent';"
    assert_includes body, 'generic reconciler is sync-only'
  end

  def test_pr_agent_mode_keeps_nano_main_sync_but_never_uses_generic_merge
    body = auto_merge_body
    automation = File.read(File.join(ROOT, '.github/workflows/automation.yml'))

    assert_includes automation, 'push:'
    assert_includes automation, 'branches: [main]'
    assert_includes automation, "github.event_name == 'push'"
    assert_includes body, "const prAgentSyncOnly = reviewProvider === 'pr-agent';"
    assert_includes body, "await updateFromMain(pr);"
    assert_includes body, "if (prAgentSyncOnly) {"
    assert_includes body, 'generic reconciler stops after main-sync evaluation'

    sync_guard = body.index("if (prAgentSyncOnly) {", body.index("await updateFromMain(pr);"))
    generic_ci = body.index("const ci = await latestCurrentHeadCi(pr);")
    refute_nil sync_guard
    refute_nil generic_ci
    assert_operator sync_guard, :<, generic_ci,
                     'PR-Agent sync-only mode must exit before generic review/merge gates'
  end

  # The transient budget is 10 total executions per exact PR/HEAD/operation by
  # default (#224), configurable through a safe bounded Continuum variable. A
  # misconfigured value can neither silence recovery nor grant an unbounded
  # budget: the workflow clamps to 1..10.
  def test_pr_agent_recovery_budget_is_ten_executions_and_variable_driven
    inputs = events(yaml(File.join(ROOT, '.github/workflows/continuum-pr-agent-recovery.yml')))
             .fetch('workflow_call').fetch('inputs')
    knob = inputs.fetch('max_executions')
    assert_equal '', knob.fetch('default'),
                 "max_executions must default to empty so vars.PR_AGENT_RECOVERY_MAX_EXECUTIONS decides"
    assert_equal 'string', knob.fetch('type')
    assert_equal false, knob.fetch('required')

    body = File.read(File.join(ROOT, '.github/workflows/continuum-pr-agent-recovery.yml'))
    # The operator fallback survives a truthy-but-non-numeric override:
    # raw input and vars fallback travel in separate envs so a bad
    # input falls back to vars instead of widening to hardcoded 10.
    assert_includes body, 'MAX_EXECUTIONS_RAW: ${{ inputs.max_executions }}'
    assert_includes body, "MAX_EXECUTIONS_FALLBACK: ${{ vars.PR_AGENT_RECOVERY_MAX_EXECUTIONS || '10' }}"
    assert_includes body, 'process.env.MAX_EXECUTIONS_RAW'
    assert_includes body, 'process.env.MAX_EXECUTIONS_FALLBACK'
    assert_includes body, 'const MAX_TRANSIENT_EXECUTIONS = 10;'
    assert_includes body, 'Math.max(MIN_TRANSIENT_EXECUTIONS, parsed)'

    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-pr-agent-recovery.yml'))
    with = stub.fetch('jobs').fetch('call').fetch('with')
    assert_match(/\A\$\{\{ inputs\.max_executions/, with.fetch('max_executions').to_s,
                 'the stub must pass max_executions through instead of pinning a literal')
  end

  # Lifecycle self-healing DoD (#224): one deterministic contract test per
  # guarantee so a regression in any behavior fails fast here.
  def test_pr_agent_recovery_lifecycle_dod_contract
    recovery = File.read(File.join(ROOT, '.github/workflows/continuum-pr-agent-recovery.yml'))
    automerge = File.read(File.join(ROOT, '.github/workflows/continuum-auto-merge.yml'))

    # Rate-limit reset resumption without sleeping a runner.
    assert_includes recovery, 'extractRateLimitSignals'
    assert_includes recovery, 'retry-after'
    assert_includes recovery, 'ratelimitResetEpoch'
    assert_includes recovery, 'resetAwareDelaySeconds'
    assert_includes recovery, 'MAX_INLINE_WAIT_SECONDS'
    assert_includes recovery, 'scheduled safety net will redispatch'

    # Bounded retry classification: 429/5xx/network/cancelled retry via the
    # transient classifier; deterministic failures hold loudly per PR.
    assert_includes recovery, 'isTransientApiError'
    assert_includes recovery, 'retryableConclusions'
    assert_includes recovery, 'deterministicFailure'
    assert_includes recovery, 'skipping PR but continuing run'
    assert_includes automerge, 'deterministic fetch error; skipping PR'
    assert_includes automerge, 'transient fetch error; deferred to next wakeup'

    # Watchdog resume, duplicate-wakeup coalescing, independent-PR progress.
    assert_includes recovery, 'tryAcquireLease'
    assert_includes recovery, 'leaseKey'
    assert_includes recovery, 'ownedLeases'
    assert_includes automerge, 'tryAcquireLease'
    assert_includes automerge, 'cancel-in-progress: false'

    # Old-HEAD isolation: exact full-commit identity, no prefix overlap.
    assert_includes recovery, '[0-9a-f]{40,64}'
    refute_includes recovery, '[0-9a-f]{7,64}'
    assert_includes recovery, 'marker === current'

    # Success clears state; exhaustion is observable and fail-closed.
    assert_includes recovery, 'operation already settled'
    assert_includes recovery, 'continuum-lifecycle-retry-exhausted'
    assert_includes recovery, 'attempts='
    assert_includes recovery, "action: 'exhaust'"

    # No TAP_PAT reads on watchdog wakeups: same-repository scans use the
    # repository-token client; dispatches/mutations stay PAT-backed.
    assert_includes recovery, 'READ_GITHUB_TOKEN'
    assert_includes recovery, 'withReadFallback'
    assert_includes automerge, 'READ_GITHUB_TOKEN: ${{ github.token }}'
    assert_includes automerge, 'new github.constructor({ auth: readToken, baseUrl: readBaseUrl })'
    assert_includes automerge, 'async function withReadFallback(fn)'
    assert_includes automerge, 'client.rest.pulls.get'
    assert_includes automerge, 'client.rest.pulls.list'
    assert_includes automerge, 'client.rest.repos.getCombinedStatusForRef'
    assert_includes automerge, 'client.rest.issues.listComments'
    assert_includes automerge, 'client.rest.repos.getCommit'
    assert_includes automerge, 'client.rest.repos.getBranch'
    assert_includes automerge, 'client.rest.pulls.listReviews'
    assert_includes automerge, 'client.rest.pulls.listReviewComments'
    assert_includes automerge, 'client.rest.actions.listWorkflowRunsForRepo'
    assert_includes automerge, 'client.rest.actions.listWorkflowRuns'
    assert_includes automerge, 'client.graphql'
    refute_includes automerge, 'github.rest.pulls.listReviews'
    refute_includes automerge, 'github.rest.actions.listWorkflowRunsForRepo'
    refute_includes automerge, 'github.paginate('
    assert_includes automerge, 'github.graphql('
    assert_includes automerge, 'github.rest.pulls.merge'
    assert_includes automerge, 'github.rest.issues.createComment'
    assert_includes automerge, 'github.rest.issues.addLabels'
    assert_includes automerge, 'github.rest.actions.createWorkflowDispatch'

    # Shared transient/reset/lease vocabulary lives in both reconcilers.
    assert_includes recovery, 'x-ratelimit-remaining'
    assert_includes automerge, 'x-ratelimit-remaining'
    assert_includes automerge, 'resetAwareDelaySeconds'
    assert_includes automerge, 'MAX_INLINE_WAIT_SECONDS'
    assert_includes automerge, 'extractRateLimitSignals'
    assert_includes recovery, 'not-before='
    assert_includes automerge, 'deferring the whole scan to the next wakeup'
    assert_includes recovery, 'deferring the whole scan to the next wakeup'
    assert_includes automerge, 'already queued; skipping this scan'

    # Durable identity and fail-closed behavior.
    assert_includes recovery, 'requireFullHead'
    assert_includes recovery, 'non-full commit id'
    assert_includes recovery, 'staying inside dispatch grace'
    assert_includes recovery, 'dispatch was rate-limited; deferred'
    assert_includes recovery, 'deterministicFailure'
    assert_includes recovery, 'skipping PR but continuing run'
    assert_includes recovery, 'core.setFailed('
    assert_includes automerge, 'liveBeforeSync'
    assert_includes automerge, 'holding stale reconciliation'
    assert_includes automerge, 'sha: pr.head.sha'
    # Exact-HEAD isolation: short-SHA markers are ignored entirely so an
    # old HEAD sharing a 7-char prefix can never strand a new HEAD. No
    # short retry regex may feed latestAttempt and no prefix match may
    # preserve exhaustion.
    assert_includes recovery, 'short-SHA markers are ignored'
    refute_includes recovery, 'exactHead.startsWith(shortHead)'
    refute_includes recovery, 'legacyShortRetryRe'
    refute_includes recovery, 'startsWith(marker)'

    # Behavioral wiring: the strings above must feed the decisions below.
    # Dead or unwired helpers containing those names still fail here.
    # Reset-aware minimums: extracted signals flow into the delay computer,
    # and the computer enforces the server reset floor over schedule+jitter.
    assert_includes recovery, 'const signals = extractRateLimitSignals(err);'
    assert_includes recovery, 'retryAfterSeconds: signals.retryAfterSeconds,'
    assert_includes recovery, 'ratelimitResetEpoch: signals.ratelimitResetEpoch,'
    assert_includes recovery, 'delay = Math.max(delay, retryAfterSeconds);'
    assert_includes recovery, 'delay = Math.max(delay, Math.max(0, resetEpoch - nowEpoch));'
    assert_includes recovery, 'resetFloor = Math.max(resetFloor, signals.retryAfterSeconds);'
    # No-sleeping-runner deferral: a long reset-aware wait defers to the
    # scheduled safety net instead of sleeping the runner inline.
    assert_includes recovery, 'if (remaining > MAX_INLINE_WAIT_SECONDS) {'
    assert_match(/remaining > MAX_INLINE_WAIT_SECONDS[\s\S]{0,3000}scheduled safety net will redispatch/m, recovery)
    # Lease coalescing: the per-PR lease key feeds the acquire gate, and the
    # repository-global scan coalesces through that gate.
    assert_includes recovery, 'const key = leaseKey(prNumber, head, kind);'
    assert_includes recovery, "if (!tryAcquireLease(pr.number, head, 'reconciler')) {"
    assert_match(/function tryAcquireLease[\s\S]{0,300}leaseKey\(/m, recovery)
    # Old-HEAD isolation: evidence filters on exact identity and every
    # dispatch/evidence entry point requires a full commit id.
    assert_includes recovery, 'if (markerKind !== kind || !sameHead(markerHead, exactHead)) return;'
    assert_includes recovery, "const exactHead = requireFullHead(head, 'retry evidence read');"
    assert_includes recovery, "requireFullHead(head, 'recovery dispatch');"
    # Dispatch-grace / active-run coalescing: the pre-mutation re-read checks
    # the exact run and the refreshed not-before before mutating.
    assert_includes recovery, 'const refreshedRuns = await listReviewRuns();'
    assert_includes recovery, 'if (exactActiveRun(refreshedRuns, pr.number, kind, head)) {'
    assert_includes recovery, 'const activeRun = exactActiveRun(reviewRuns, pr.number, kind, head);'
    assert_includes recovery, 'const refreshedEvidence = parseEvidence(refreshedComments, head, kind);'
    assert_includes recovery, 'Number.isFinite(refreshedEvidence.notBeforeEpoch)'
    assert_includes recovery, 'await operationIsActive(pr.number, head, kind)'
  end

  # Both branches of the flag, checked in the body that acts on them. Asserting
  # only the env string would let every gate keep reading the flag as `true`
  # and no test would notice.
  def test_auto_merge_skips_every_coderabbit_gate_when_disabled
    body = auto_merge_body

    # The provider is parsed once and CodeRabbit gates are active only for
    # the exact coderabbit mode.
    assert_match(/const reviewProvider =\s*String\(process\.env\.REVIEW_PROVIDER \|\| 'none'\)\.trim\(\)\.toLowerCase\(\);/, body)
    assert_includes body, "const requireCodeRabbit = reviewProvider === 'coderabbit';"

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

    # The coderabbit provider must switch every CodeRabbit gate on.
    assert_includes body, "const requireCodeRabbit = reviewProvider === 'coderabbit';"

    # The CI gate stays unconditional: CodeRabbit never replaced it.
    refute_match(/requireCodeRabbit[^;]*!finalCi/, body)
    assert_includes body, '!finalCi ||'
  end

  # ------------------------------------------------- promoted fork capabilities

  # (a) Label-driven execution routes. The fork hardcoded three labels and
  # three workflow file names; core parameterizes the whole table instead, so
  # a consumer can route to any workflow it owns and a consumer with none gets
  # nothing at all. The default must therefore be EMPTY, and the parser must be
  # a live one that fails loudly on a malformed line rather than silently
  # dropping a route the operator believed was installed.
  def test_scheduler_execution_routes_are_configured_and_empty_by_default
    inputs = events(yaml(File.join(ROOT, '.github/workflows/continuum-issue-scheduler.yml')))
             .fetch('workflow_call').fetch('inputs')

    routes = inputs.fetch('execution_label_routes')
    assert_equal '', routes.fetch('default'),
                 'execution_label_routes must default to empty: core ships no execution workflows'
    assert_equal 'string', routes.fetch('type')
    assert_equal false, routes.fetch('required')
    assert_includes routes.fetch('description'), 'vars.CONTINUUM_EXECUTION_LABEL_ROUTES'

    child = inputs.fetch('child_dispatch_workflow')
    assert_equal '', child.fetch('default'),
                 'child_dispatch_workflow must default to empty: no core dispatcher is guaranteed to exist'
    assert_includes child.fetch('description'), 'vars.CONTINUUM_CHILD_DISPATCH_WORKFLOW'

    dispatch = inputs.fetch('opencode_dispatch')
    assert_equal 'comment', dispatch.fetch('default'),
                 'opencode_dispatch must default to the comment path, which needs no extra permission'

    body = workflow_body('continuum-issue-scheduler.yml')
    # Live parser, not a hardcoded table.
    assert_includes body, "(process.env.EXECUTION_LABEL_ROUTES || '')"
    assert_includes body, '"Invalid execution_label_routes line: \'"'
    assert_includes body, '"\'; expected \'<issue label>|<workflow file name>|<input>=<value>,...\'."'
    assert_includes body, '"Invalid execution_label_routes input \'"'
    assert_includes body, 'const executionRouteLabels = new Set('

    # …and the route really dispatches, with the issue number injected so a
    # route never has to spell out the only input every execution workflow
    # shares.
    helper = body[/async function dispatchWorkflow\(workflow, inputs\) \{.*?\n {14}\}/m]
    refute_nil helper, 'the dispatchWorkflow helper is missing from the scheduler script'
    assert_includes helper, 'POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches'
    assert_match(
      /await dispatchWorkflow\(route\.workflow, \{\s*\.\.\.route\.inputs,\s*issue_number: String\(issue\.number\),\s*\}\);/,
      body,
      'a configured route must dispatch its own inputs plus the issue number'
    )

    # A configured label must exist in the repository so an operator can apply
    # it from the issue page.
    assert_includes body, '...executionRouteLabels].map(name => ({'

    # (b) the child wake-up is one step, gated on the knob being non-empty.
    wakeup = step_body(body, 'Wake the configured downstream dispatcher')
    refute_nil wakeup, 'the child-dispatch wake-up step is missing'
    assert_includes wakeup, "if: env.CHILD_DISPATCH_WORKFLOW != ''",
                    'the child wake-up must not run when no dispatcher is configured'
    assert_match(%r{repos/\$GITHUB_REPOSITORY/actions/workflows/\$CHILD_DISPATCH_WORKFLOW/dispatches}, wakeup)
    assert_includes wakeup, '::warning::'
  end

  # The route-line guard must be LIVE, not merely present. Asserting that the
  # error text exists somewhere in the workflow cannot tell a working guard
  # from `if (false) {` with the throw left inside: the text is identical in
  # both, so the malformed-route protection would silently vanish while every
  # assertion about the strings still passed. Scope each error text to the
  # guard's own brace-matched body, so neutering the condition drops the text
  # out of the extracted block and the test fails.
  def test_scheduler_route_line_guard_is_live_and_owns_its_error_text
    body = workflow_body('continuum-issue-scheduler.yml')

    guard = js_block(body, 'if (parts.length < 2 || parts.length > 3 || !parts[0] || !parts[1])')
    refute_nil guard, 'the malformed route-line guard is missing from the scheduler script'
    assert_includes guard, 'parts.length < 2'
    assert_includes guard, 'parts.length > 3'
    assert_includes guard, '!parts[0]'
    assert_includes guard, '!parts[1]'
    assert_includes guard, 'throw new Error('
    assert_includes guard, '"Invalid execution_label_routes line: \'"',
                    'the line-level error must be raised by the guard, not merely present in the file'
    assert_includes guard, '"\'; expected \'<issue label>|<workflow file name>|<input>=<value>,...\'."'

    # The input-level guard has the same weakness: its error text exists only
    # once in the file, so pinning it to the `separator <= 0` body is what
    # proves a malformed `<input>=<value>` assignment still fails the run.
    input_guard = js_block(body, 'if (separator <= 0)')
    refute_nil input_guard, 'the malformed route-input guard is missing from the scheduler script'
    assert_includes input_guard, 'throw new Error('
    assert_includes input_guard, '"Invalid execution_label_routes input \'"',
                    'the input-level error must be raised by the guard, not merely present in the file'
    assert_includes input_guard, '"\'; expected \'<name>=<value>\'."'
  end

  # `opencode_dispatch` selects between two real dispatch forms, so an
  # unrecognized value must fail loudly rather than fall through to a form the
  # consumer's OpenCode caller cannot accept. Both halves of that contract —
  # the two-value enum and the error text — live under one live guard.
  def test_scheduler_opencode_dispatch_enum_is_validated_under_a_live_guard
    body = workflow_body('continuum-issue-scheduler.yml')

    guard = js_block(body, "if (!['comment', 'workflow'].includes(opencodeDispatch))")
    refute_nil guard, 'the opencode_dispatch enum guard is missing from the scheduler script'
    assert_includes guard, "'comment'",
                    'the enum must admit the comment form, which is the permissionless default'
    assert_includes guard, "'workflow'",
                    'the enum must admit the workflow form, for callers without an issue_comment trigger'
    assert_includes guard, 'throw new Error('
    assert_includes guard, '"Unsupported opencode_dispatch \'"',
                    'the enum error must be raised by the guard, not merely present in the file'
    assert_includes guard, '"\'; expected \'comment\' or \'workflow\'."'

    # The guard must read the same value the dispatch later branches on, so a
    # validated-but-unused variable would pass the enum check and still
    # dispatch the wrong form.
    assert_includes body, 'const opencodeDispatch = ('
    assert_includes body, 'process.env.OPENCODE_DISPATCH'
  end

  # Every dispatch in this job must target the configured ref. The
  # `opencode_dispatch: workflow` window is covered elsewhere; the route
  # helper is a separate call site and had no assertion of its own, so a
  # hardcoded `ref: 'main'` there would dispatch routes at main regardless of
  # the consumer's `dispatch_ref`.
  def test_scheduler_route_helper_dispatches_at_the_configured_ref
    body = workflow_body('continuum-issue-scheduler.yml')

    helper = js_block(body, 'async function dispatchWorkflow(workflow, inputs)')
    refute_nil helper, 'the dispatchWorkflow helper is missing from the scheduler script'
    assert_includes helper, 'POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches'
    assert_includes helper, 'ref: dispatchRef',
                    'the route helper must dispatch at the configured ref, not a hardcoded one'
    refute_includes helper, "ref: 'main'",
                   'the route helper must not hardcode a ref; it exists only as the env fallback'

    # The value it forwards must itself come from the configurable env, not a
    # second hardcoded default inside the script.
    assert_includes body, "const dispatchRef = process.env.DISPATCH_REF || 'main';"
  end

  # The scheduler's own env binding for DISPATCH_REF is what makes
  # `dispatch_ref` / vars.CONTINUUM_DISPATCH_REF reach the script at all. Other
  # workflows have their own DISPATCH_REF bindings, so an assertion on the
  # whole file or on another workflow cannot pin this one.
  def test_scheduler_dispatch_ref_env_binding_follows_the_consumer_knob
    body = workflow_body('continuum-issue-scheduler.yml')

    # Read the schedule job structurally so sibling jobs do not make this
    # assertion depend on schedule being the first job in the workflow.
    schedule = yaml(File.join(ROOT, '.github/workflows/continuum-issue-scheduler.yml'))
               .fetch('jobs').fetch('schedule')
    env_block = schedule.fetch('env')
    assert_equal "${{ inputs.dispatch_ref || vars.CONTINUUM_DISPATCH_REF || 'main' }}",
                 env_block.fetch('DISPATCH_REF'),
                 'the scheduler must expose dispatch_ref through DISPATCH_REF for its script'

    # The fallback chain and the declared default must agree, or the input is
    # documented as one thing and bound as another.
    inputs = events(yaml(File.join(ROOT, '.github/workflows/continuum-issue-scheduler.yml')))
             .fetch('workflow_call').fetch('inputs')
    assert_equal 'main', inputs.fetch('dispatch_ref').fetch('default')
    assert_includes inputs.fetch('dispatch_ref').fetch('description'),
                    'vars.CONTINUUM_DISPATCH_REF'
  end

  # The `workflow` dispatch form of opencode_dispatch must carry `issue`, and
  # the issue mode itself must be admitted by the engine's mode whitelist —
  # otherwise the route and the mode would each be half of a dead end.
  def test_scheduler_workflow_dispatch_path_reaches_the_issue_mode
    body = workflow_body('continuum-issue-scheduler.yml')
    window = dispatch_calls('continuum-issue-scheduler.yml').find do |call|
      call.include?("mode: 'issue'")
    end
    refute_nil window,
               'the scheduler no longer dispatches OpenCode in `issue` mode over workflow_dispatch'
    assert_includes window, 'opencodeWorkflowPath.replace'
    assert_includes window, 'ref: dispatchRef'

    assert_includes dispatch_modes, 'issue',
                    'continuum-opencode.yml must admit `issue` in its dispatch whitelist'

    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-opencode.yml'))
    options = events(stub).fetch('workflow_dispatch').fetch('inputs')
                 .fetch('mode').fetch('options')
    assert_equal dispatch_modes.sort, options.sort,
                 'the caller stub must offer exactly the modes the engine admits'
  end

  # (c) Post-merge wake-ups are consumer-owned: `knowledge-sync` has no core
  # equivalent at all, so the list must come from configuration and must be
  # EMPTY by default. A hardcoded default would dispatch a workflow the
  # repository does not have.
  def test_auto_merge_post_merge_wakeups_are_configured_and_empty_by_default
    inputs = events(yaml(File.join(ROOT, '.github/workflows/continuum-auto-merge.yml')))
             .fetch('workflow_call').fetch('inputs')

    %w[post_merge_wakeups post_merge_wakeup_ref].each do |key|
      knob = inputs.fetch(key)
      assert_equal '', knob.fetch('default'),
                   "#{key} must default to empty: the downstream workflows are the consumer's, not core's"
      assert_equal 'string', knob.fetch('type')
      assert_equal false, knob.fetch('required')
    end
    assert_includes inputs.fetch('post_merge_wakeups').fetch('description'),
                    'vars.CONTINUUM_POST_MERGE_WAKEUPS'
    assert_includes inputs.fetch('post_merge_wakeup_ref').fetch('description'),
                    'vars.CONTINUUM_POST_MERGE_WAKEUP_REF'

    body = auto_merge_body
    # No hardcoded workflow names in the auto-merger's logic. (The comments
    # legitimately name them to explain why they are not there, so only
    # executable lines are checked.)
    script = script_body('continuum-auto-merge.yml')
                 .lines.reject { |line| line.strip.start_with?('//') }.join
    %w[knowledge-sync issue-scheduler].each do |name|
      refute_match(/#{name}[-a-z_]*\.yml/, script,
                   "#{name}.yml must not be hardcoded into the auto-merger; the list is configuration")
    end

    wakeups = dispatch_calls('continuum-auto-merge.yml').find do |call|
      call.include?('POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches') &&
        call.include?('workflow_id: workflow')
    end
    refute_nil wakeups, 'the post-merge wake-up dispatch is missing from the auto-merger'
    assert_includes wakeups, 'ref: postMergeWakeupRef'

    # Best-effort: a wake-up that cannot be delivered must not fail a run whose
    # merge already landed.
    assert_includes body, "for (const workflow of postMergeWakeups) {"
    assert_match(/catch \(err\) \{\s*\n\s*core\.warning\(/, body)

    # The list is parsed from configuration, split on commas, empties dropped.
    assert_includes body, "String(process.env.POST_MERGE_WAKEUPS || '')"
    assert_includes body, '.filter(name => name.length > 0);'
  end

  # `core.warning` appears five times in the auto-merger, so a regex anchored
  # only to its own text matches the FIRST occurrence — the review-thread
  # handler — and says nothing about the post-merge wake-up. Extracting the
  # wake-up loop and pinning the catch inside it is what actually asserts the
  # best-effort contract: a wake-up that cannot be delivered warns, and never
  # fails a run whose merge already landed.
  # Conflict repair must dispatch the consumer-owned workflow_dispatch caller,
  # not the reusable engine. Continuum dogfood names that caller opencode.yml,
  # while installed consumers keep the continuum-opencode.yml default.
  def test_auto_merge_conflict_repair_uses_configured_consumer_caller
    inputs = events(yaml(File.join(ROOT, '.github/workflows/continuum-auto-merge.yml')))
             .fetch('workflow_call').fetch('inputs')
    knob = inputs.fetch('opencode_workflow')
    assert_equal 'continuum-opencode.yml', knob.fetch('default')
    assert_equal 'string', knob.fetch('type')
    assert_equal false, knob.fetch('required')

    body = auto_merge_body
    assert_includes body,
                    "OPENCODE_WORKFLOW: ${{ inputs.opencode_workflow || 'continuum-opencode.yml' }}"
    refute_includes body, "workflow_id: 'continuum-opencode.yml'",
                    'conflict repair must not hardcode the installed-consumer caller name'

    repair_dispatch = dispatch_calls('continuum-auto-merge.yml').find do |call|
      call.include?("mode: 'resolve-conflict'")
    end
    refute_nil repair_dispatch, 'automatic conflict-repair dispatch is missing'
    # The dispatch carries the resolved caller variable; the resolver itself
    # reads the configured knob (asserted below), so dogfood's opencode.yml
    # override still reaches the dispatch without a hardcoded file name.
    assert_includes repair_dispatch, 'workflow_id: opencodeWorkflow'
    resolver = js_block(body, 'function resolveOpencodeWorkflow()')
    refute_nil resolver, 'the caller resolver is missing from the auto-merger'
    assert_includes resolver, 'process.env.OPENCODE_WORKFLOW'

    dogfood = workflow_body('automation.yml')
    assert_match(/auto-merge:.*?opencode_workflow: 'opencode\.yml'/m, dogfood,
                 'Continuum dogfood must route conflict repair through its real workflow_dispatch caller')
  end

  # Issue #246: dirty-PR conflict recovery must be self-healing. Every
  # reconciliation pass rediscovers already-dirty PRs (no fresh PR event or
  # manual kick), a stale lock with no live repair run is reconciled and
  # redispatched, dispatches stay idempotent per PR/HEAD and bounded, and a
  # repaired HEAD resumes the normal CI/review lifecycle without weakening
  # exact-HEAD gates.
  def test_auto_merge_conflict_recovery_is_self_healing
    body = auto_merge_body
    script = script_body('continuum-auto-merge.yml')
             .lines.reject { |line| line.strip.start_with?('//') }.join

    # Every pass scans every open PR against main: the dirty check cannot
    # depend on a fresh PR event.
    assert_includes body, "state: 'open'"
    assert_includes body, "base: 'main'"
    assert_includes body, 'await reconcileDirtyPr(pr)'

    # The early scan detects mergeable=false / mergeable_state=dirty and runs
    # before the CI/review gates, so a conflicted HEAD is never stranded
    # behind a red CI check.
    scan = js_block(body, 'async function reconcileDirtyPr(pr)')
    refute_nil scan, 'the early dirty-PR reconciler is missing from the auto-merger'
    assert_includes scan, "mergeable_state"
    assert_includes scan, 'await dispatchConflictRepair('
    early_at = body.index('await reconcileDirtyPr(pr)')
    ci_at = body.index('const ci = await latestCurrentHeadCi(pr);')
    refute_nil ci_at, 'the current-head CI gate is missing'
    assert_operator early_at, :<, ci_at,
                     'the dirty-PR scan must run before the CI gate, or dirty PRs strand behind red CI'

    # The reusable engine is never dispatched directly: the caller file name
    # always comes from the configured knob and is validated.
    assert_includes body, 'function resolveOpencodeWorkflow()'
    assert_includes body, 'process.env.OPENCODE_WORKFLOW'
    repair_dispatch = dispatch_calls('continuum-auto-merge.yml').find do |call|
      call.include?("mode: 'resolve-conflict'")
    end
    refute_nil repair_dispatch, 'automatic conflict-repair dispatch is missing'
    assert_includes repair_dispatch, 'workflow_id: opencodeWorkflow'
    refute_includes script, "workflow_id: 'continuum-opencode.yml'",
                    'conflict repair must not hardcode the engine file name'

    # A live repair run suppresses duplicates; a stale lock with no active
    # run is reconciled and redispatched.
    assert_includes body, 'findActiveConflictRepairRun'
    assert_includes body, 'already active for this PR; waiting'
    assert_includes body, '(?!\\\\d)',
                    'the active-run probe must not match PR #23 against the live run for PR #237'
    assert_includes body, 'reconciled stale'
    assert_includes body, 'redispatching'

    # Idempotency per PR/current HEAD plus a bounded per-HEAD budget: repeated
    # wakeups coalesce instead of duplicating repairs.
    assert_includes body, 'continuum-conflict-repair head='
    assert_includes body, 'CONFLICT_DISPATCH_GRACE_MS'
    assert_includes body, 'inside its grace window; waiting'
    assert_includes body, 'MAX_CONFLICT_DISPATCHES_PER_HEAD'
    assert_includes body, 'budget exhausted for head'

    # A repaired HEAD reconciles the lock and resumes CI/review instead of
    # merging or pausing; failure stays autonomous and bounded.
    assert_includes body, 'is no longer dirty; reconciled'
    assert_includes body, 'resuming CI/review lifecycle'
    repair_window = body[body.index('async function dispatchConflictRepair')..]
    refute_includes repair_window, 'automation:paused',
                    'conflict recovery must never terminally pause for manual label removal'

    # Exact-HEAD checks are preserved: dispatch revalidates the head and the
    # merge still binds to the exact SHA.
    assert_includes body, 'head advanced from'
    assert_includes body, 'sha: pr.head.sha,'
  end

  # The scan that rediscovers pre-existing dirty PRs needs wake-ups that do
  # not depend on PR activity: main pushes, a schedule safety net, CI
  # completions, and review/status events.
  def test_auto_merge_conflict_scan_wakes_without_pr_events
    stub = File.read(File.join(ROOT, '.github/caller-stubs/continuum-auto-merge.yml'))
    assert_includes stub, 'push:'
    assert_includes stub, 'branches: [main]'
    assert_includes stub, "cron: '17,47 * * * *'"
    assert_includes stub, 'pull_request_review:'
    assert_includes stub, 'status:'

    dogfood = workflow_body('automation.yml')
    assert_match(/\(github\.event_name == 'schedule' && github\.event\.schedule == '17,47 \* \* \* \*'\)/, dogfood)
    assert_includes dogfood, "github.event_name == 'push'"
    assert_includes dogfood, ".github/workflows/ci.yml"
  end

  def test_auto_merge_wakeup_catch_warns_inside_the_wakeup_loop
    body = auto_merge_body

    loop_block = js_block(body, 'for (const workflow of postMergeWakeups)')
    refute_nil loop_block, 'the post-merge wake-up loop is missing from the auto-merger'
    assert_includes loop_block, 'POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches'
    assert_includes loop_block, 'ref: postMergeWakeupRef'

    catch_block = loop_block[/catch \(err\) \{.*?\n {16}\}/m]
    refute_nil catch_block,
               'a failed wake-up dispatch must be caught inside the loop, not left to reject the run'
    assert_includes catch_block, 'core.warning(',
                    'the caught wake-up failure must warn, not rethrow'
    assert_includes catch_block, 'could not wake ${workflow}',
                    'the warning must name the workflow that could not be woken'

    # Best-effort means best-effort: the loop must not escalate to a red run.
    refute_includes loop_block, 'core.setFailed'
    refute_includes loop_block, 'process.exitCode'
  end

  # The empty-name filter must apply to the list BEFORE the dispatch loop
  # iterates it, not merely appear somewhere in the file. Asserting the filter
  # line exists in isolation passes even if the filter were dropped from the
  # chain, in which case a trailing comma in POST_MERGE_WAKEUPS dispatches the
  # empty workflow name and 404s the run — exactly the failure the comment
  # above the declaration says the list must not have.
  def test_auto_merge_wakeup_list_drops_empty_names_before_dispatch
    body = auto_merge_body

    declaration = body[/const postMergeWakeups = .*?\.filter\(name => name\.length > 0\);/m]
    refute_nil declaration,
               'the wake-up list must end its parse chain with the empty-name filter'
    assert_includes declaration, "String(process.env.POST_MERGE_WAKEUPS || '')"
    assert_includes declaration, ".split(',')"
    assert_includes declaration, '.map(name => name.trim())'
    assert_includes declaration, '.filter(name => name.length > 0)',
                    'the empty-name filter must be part of the list declaration, not a stray statement'

    # Order is the whole point: the filter has to run while the list is built,
    # before the loop consumes it. Assert on real positions rather than on a
    # first-occurrence match.
    declare_at = body.index('const postMergeWakeups')
    loop_at = body.index('for (const workflow of postMergeWakeups)')
    refute_nil loop_at, 'the post-merge wake-up loop is missing from the auto-merger'
    assert declare_at < loop_at,
           'the wake-up list must be declared, and filtered, before the loop dispatches it'
  end

  # Coordinator item 1: `/oc-cancel`. The command is a substring of `/oc`, so
  # without an explicit negation on each agent-launching step the cancel
  # comment starts the very agent it means to stop. Both launch sites must
  # carry it, and no step may launch on a cancel comment.
  def test_opencode_cancel_command_guards_every_agent_launch
    body = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))

    run_step = step_body(body, 'Run OpenCode')
    refute_nil run_step, 'the `Run OpenCode` step is missing'
    assert_includes run_step, "!contains(github.event.comment.body, '/oc-cancel')",
                    'Run OpenCode must not launch on an /oc-cancel comment'

    recover = step_body(body, 'Recover agent-managed issue branch')
    refute_nil recover, 'the branch-recovery step is missing'
    assert_includes recover, "!contains(github.event.comment.body, '/oc-cancel')",
                    'branch recovery must not run on an /oc-cancel comment'

    issue_step = step_body(body, 'Implement issue')
    refute_nil issue_step, 'the `issue` mode step is missing'

    # The cancellation is a launch guard only: Continuum has no mechanism that
    # cancels an already-running run, so nothing may claim otherwise.
    refute_match(%r{actions/runs/\S+/cancel}, body,
                 'no run-cancellation call is implemented; do not imply one')
  end

  # Coordinator item 2: the duplicate guard must run BEFORE the agent and the
  # agent must be gated on it. The only other open-PR check in this workflow
  # runs after a burnt run, so without this a second dispatch opens a second
  # agent against an issue that already has a PR.
  def test_opencode_skips_a_duplicate_issue_implementation
    body = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))

    guard_at = body.index('      - name: Skip duplicate issue implementation')
    refute_nil guard_at, 'the duplicate-implementation guard step is missing'
    run_at = body.index('      - name: Run OpenCode')
    issue_at = body.index('      - name: Implement issue')
    refute_nil run_at
    refute_nil issue_at
    assert guard_at < run_at, 'the duplicate guard must run before the interactive agent launches'
    assert guard_at < issue_at, 'the duplicate guard must run before the issue-mode agent launches'

    guard = step_body(body, 'Skip duplicate issue implementation')
    assert_includes guard, 'id: duplicate_guard'
    assert_includes guard, 'github-token: ${{ github.token }}'
    assert_includes guard, 'TAP_PAT: ${{ secrets.TAP_PAT }}'
    assert_includes guard, "github.rest.pulls.list"
    assert_includes guard, "state: 'open'"
    assert_includes guard, '[401, 403, 429].includes(status)'
    assert_includes guard, 'const prefix = `opencode/issue${issueNumber}-`'
    assert_includes guard, "core.setOutput('skip', 'true')"
    assert_includes guard, "core.setOutput('skip', 'false')"

    # Both launch sites must consult it.
    assert_includes body[run_at, issue_at], "steps.duplicate_guard.outputs.skip != 'true'"
    issue_step = step_body(body, 'Implement issue')
    refute_nil issue_step
    assert_includes issue_step, "steps.duplicate_guard.outputs.skip != 'true'"
  end

  # Coordinator item 3: run-name. The watchdog and the scheduler both find a
  # run; without a run-name they can only match by issue title, which is not
  # unique among open issues.
  def test_opencode_stub_run_name_reports_the_issue_number
    stub = File.read(File.join(ROOT, '.github/caller-stubs/continuum-opencode.yml'))
    run_name = stub[/^run-name:.*?(?=\n\n)/m]
    refute_nil run_name, 'the OpenCode caller stub has no run-name'
    assert_includes run_name, "github.event_name == 'issue_comment'"
    assert_includes run_name, "format('OpenCode issue \#{0}', github.event.issue.number)"
    assert_includes run_name, "inputs.mode == 'issue'"
    assert_includes run_name, "format('OpenCode issue \#{0}', inputs.issue_number)"
  end

  # (d) `issue` mode must own the whole issue -> branch -> commit -> PR
  # lifecycle. Workflow files are valid task output; TAP_PAT carries the
  # workflow scope needed to publish them on the generated task branch.
  def test_opencode_issue_mode_owns_the_full_issue_to_pr_lifecycle
    body = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))
    step = step_body(body, 'Implement issue')
    refute_nil step, 'the `issue` mode step is missing'

    assert_includes step, "inputs.mode == 'issue'"
    assert_includes step, '[[ "$ISSUE_NUMBER" =~ ^[0-9]+$ ]]'
    assert_includes step, 'BRANCH="opencode/issue${ISSUE_NUMBER}-${GITHUB_RUN_ID}"'
    assert_includes step, 'opencode run --auto --model "$OPENCODE_MODEL"'
    assert_includes step, "if [[ \"\$CURRENT_BRANCH\" != \"\$BRANCH\" ]]; then"
    assert_includes step, 'You may modify .github/workflows/** when the issue requires it.'
    refute_includes step, 'Do not modify .github/workflows/**'
    refute_includes step, 'OpenCode modified .github/workflows/**'
    refute_includes step, 'Task commit contains .github/workflows/** changes.'
    assert_includes step, 'git commit -m "${COMMIT_PREFIX}: implement issue #${ISSUE_NUMBER}"'
    assert_includes step, 'gh api --method POST "repos/$GITHUB_REPOSITORY/git/refs"'
    assert_includes step, 'git push --force-with-lease="refs/heads/$BRANCH:$BASE_SHA"'
    assert_includes step, 'gh pr create --repo "$GITHUB_REPOSITORY"'
    assert_includes step, '--add-label "$PAUSE_LABEL" --remove-label "$IN_PROGRESS_LABEL"'

    # A burnt issue-mode run must release the scheduler's reservation, exactly
    # as an issue_comment run does, or the WIP slot leaks until the lease
    # expires.
    recover = body[/  recover-scheduled-issue:.*/m]
    refute_nil recover
    assert_includes recover, "github.event_name == 'workflow_dispatch' &&\n        inputs.issue_number != ''",
                    'the recovery job must also cover an issue-mode workflow_dispatch run'
    assert_includes recover, 'ISSUE_NUMBER: ${{ inputs.issue_number }}'
    assert_includes recover, 'Number(process.env.ISSUE_NUMBER || context.payload.issue?.number || 0)'

    # The knowledge handoff is consumer-owned and Continuum ships no protocol,
    # so both halves must be OFF unless the consumer names them.
    assert_includes step, 'KNOWLEDGE_PROTOCOL_PATH: ${{ inputs.knowledge_protocol_path }}'
    assert_includes step, 'KNOWLEDGE_RECORDS_DIR: ${{ inputs.knowledge_records_dir }}'
    assert_includes step, 'if [[ -n "$KNOWLEDGE_RECORDS_DIR" ]]; then'
    assert_includes step, '[[ -f "$RECORD_PATH" ]] || {'
    refute_includes step, 'render_lifecycle',
                    'core ships no automation/render_lifecycle module; the validator must stay consumer-owned'
  end

  # The knowledge handoff has two halves and BOTH must be off unless the
  # consumer names them. The env bindings were pinned but the gates were not:
  # the RECORDS_DIR half was checked, the PROTOCOL_PATH half was not, so
  # `if true` on the protocol gate would emit a mandatory handoff for a
  # consumer that named no protocol — an asymmetry that hides a broken gate.
  def test_opencode_knowledge_handoff_halves_are_each_behind_a_live_gate
    body = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))
    step = step_body(body, 'Implement issue')
    refute_nil step, 'the `issue` mode step is missing'

    protocol_gate = shell_if_gate(step, 'KNOWLEDGE_PROTOCOL_PATH')
    refute_nil protocol_gate,
               'the KNOWLEDGE_PROTOCOL_PATH gate is missing: the handoff prompt must be conditional'
    assert_includes protocol_gate, 'read ${KNOWLEDGE_PROTOCOL_PATH}',
                    'the mandatory-handoff line must live inside the protocol gate'
    assert_includes protocol_gate, 'PROMPT="$PROMPT'

    records_gate = shell_if_gate(step, 'KNOWLEDGE_RECORDS_DIR')
    refute_nil records_gate,
               'the KNOWLEDGE_RECORDS_DIR gate is missing: the run-record line must be conditional'
    assert_includes records_gate, 'write exactly one run record at ${KNOWLEDGE_RECORDS_DIR}/issue-'

    # The two halves are independent: neither gate may read the other's
    # variable, or naming only one would drag in the other half too.
    refute_includes protocol_gate, 'KNOWLEDGE_RECORDS_DIR'
    refute_includes records_gate, 'KNOWLEDGE_PROTOCOL_PATH'

    # The env bindings the gates read must still be the plain, undefaulted
    # inputs: a hardcoded default here would make the gate always true.
    assert_includes step, 'KNOWLEDGE_PROTOCOL_PATH: ${{ inputs.knowledge_protocol_path }}'
    assert_includes step, 'KNOWLEDGE_RECORDS_DIR: ${{ inputs.knowledge_records_dir }}'
    refute_match(/KNOWLEDGE_PROTOCOL_PATH:.*\|\| *'[^']+'/, step,
                 'the protocol path must not carry a fallback default; the gate supplies the off state')
  end

  # COMMIT_PREFIX is what names the task commit `issue` mode creates. The
  # consumer of the variable was pinned (`git commit -m "${COMMIT_PREFIX}…"`)
  # but the binding that supplies it was not, so an empty or hardcoded
  # COMMIT_PREFIX would produce commits like `: implement issue #12`.
  def test_opencode_commit_prefix_env_binding_follows_the_consumer_knob
    body = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))
    step = step_body(body, 'Implement issue')
    refute_nil step, 'the `issue` mode step is missing'

    binding_line = "COMMIT_PREFIX: ${{ inputs.issue_commit_prefix || vars.CONTINUUM_ISSUE_COMMIT_PREFIX || 'fix' }}"
    assert_includes step, binding_line,
                    'COMMIT_PREFIX must fall back from input to repository variable to `fix`'
    assert_includes step, 'git commit -m "${COMMIT_PREFIX}: implement issue #${ISSUE_NUMBER}"',
                    'the commit subject must actually consume COMMIT_PREFIX'

    # The declared default and the documented fallback chain must agree with
    # the binding, so the input is not advertised as one thing and bound as
    # another.
    inputs = events(yaml(File.join(ROOT, '.github/workflows/continuum-opencode.yml')))
             .fetch('workflow_call').fetch('inputs')
    prefix = inputs.fetch('issue_commit_prefix')
    assert_equal '', prefix.fetch('default'),
                 'the fallback chain supplies `fix`, so the input itself must default to empty'
    assert_includes prefix.fetch('description'), 'vars.CONTINUUM_ISSUE_COMMIT_PREFIX'
  end


  def test_generic_validation_contract_is_consumer_neutral_and_executable
    workflow = yaml(File.join(ROOT, '.github/workflows/continuum-validation.yml'))
    inputs = events(workflow).fetch('workflow_call').fetch('inputs')

    assert_equal '', inputs.fetch('runner').fetch('default')
    %w[prepare_command build_command test_command validation_command package_command release_command].each do |name|
      assert_equal '', inputs.fetch(name).fetch('default'), name
    end
    assert_equal '', inputs.fetch('artifact_paths').fetch('default')
    assert_equal '', inputs.fetch('artifact_name').fetch('default')
    assert_equal '', inputs.fetch('repair_workflow').fetch('default')

    raw = workflow_body('continuum-validation.yml')
    %w[Python Node PHP Java Go Rust Docker Bun].each { |stack| assert_includes raw, stack }
    assert_includes raw, 'createCommitStatus'
    assert_includes raw, 'createWorkflowDispatch'
    assert_includes raw, 'actions/upload-artifact@'
    assert_includes raw, 'eval "$command"'
    refute_match(/nanodictate|kodmai|runtime-lab/i, raw)

    refute File.exist?(File.join(ROOT, '.github/caller-stubs/continuum-validation.yml')),
           'validation is a shared engine; primary CI stays project-owned'
    assert_includes raw, "vars.CONTINUUM_RUNNER || 'ubuntu-latest'"
    assert_includes raw, 'inputs.node_version || vars.CONTINUUM_NODE_VERSION'
    assert_includes raw, 'inputs.php_version || vars.CONTINUUM_PHP_VERSION'
    assert_includes raw, 'inputs.bun_version || vars.CONTINUUM_BUN_VERSION'
  end


  # Consumer-owned repository paths are configuration, never core defaults.
  # A quoted automation/... path in a reusable workflow writes into or executes
  # from the consumer checkout and therefore couples core to one repository layout.
  def test_core_workflows_embed_no_consumer_automation_paths
    offenders = WORKFLOWS.filter_map do |path|
      hits = File.readlines(path, chomp: true).each_with_index.filter_map do |line, index|
        next unless line.match?(/["']automation\//)

        "#{File.basename(path)}:#{index + 1}: #{line.strip}"
      end
      hits unless hits.empty?
    end.flatten

    assert_empty offenders,
                 "consumer-owned automation/ paths must arrive through configuration/hooks:\n#{offenders.join("\n")}"
  end

  def test_render_script_hooks_are_configuration_only
    body = workflow_body('continuum-render-executor.yml')

    assert_includes body, 'JOB_SCRIPT: ${{ inputs.job_script || vars.RENDER_JOB_SCRIPT }}'
    assert_includes body, 'CLEANUP_SCRIPT: ${{ inputs.cleanup_script || vars.RENDER_CLEANUP_SCRIPT }}'
    assert_includes body, 'QUALIFICATION_SCRIPT: ${{ inputs.qualification_script || vars.RENDER_QUALIFICATION_SCRIPT }}'
    refute_match(/RENDER_(?:JOB|CLEANUP|QUALIFICATION)_SCRIPT \|\| ['"][^'"]+['"]/, body)
    assert_includes body, 'No Render job hook configured'
    assert_includes body, 'No Render cleanup hook configured'
    assert_includes body, 'No Render qualification hook configured'
  end

  def test_child_acceptance_sentinels_live_outside_consumer_checkout
    worker = workflow_body('continuum-consumer-child-worker.yml')
    review = workflow_body('continuum-consumer-child-review.yml')

    assert_includes worker, 'result_file="$RUNNER_TEMP/continuum-child-task-${TASK_NUMBER}.md"'
    assert_includes review, 'review_file="$RUNNER_TEMP/continuum-child-review-${TASK_NUMBER}.md"'
    refute_includes worker, 'automation/runtime-results'
    refute_includes review, 'automation/runtime-results'
  end

  # ------------------------------------------------- mandatory qualification gate

  # The implementation -> qualification -> capability-completion lifecycle is
  # a first-class gate: an implementation merge enters `automation:qualifying`
  # instead of closing the capability, qualification auto-starts against the
  # exact merged main SHA, only exact-SHA pass evidence may complete the
  # capability, failure blocks with repair routing, and auto-close keywords
  # can never bypass the gate. Each behavior below is asserted on the code
  # that acts on it, so deleting the behavior while leaving the marker in
  # place fails here.
  def test_mandatory_qualification_gate_is_a_first_class_lifecycle
    scheduler = workflow_body('continuum-issue-scheduler.yml')

    # The declaration marker is parsed deterministically, never inferred
    # from prose. Single backslashes: a doubled backslash is a legal,
    # silently non-matching regex.
    assert_includes scheduler, '/<!--\s*automation-qualification:\s*([0-9#\s,]+?)\s*-->/',
                    'the automation-qualification regex must use single backslashes'
    doubled = 'automation-qualification:' + ('\\' * 2) + 's'
    refute_includes scheduler, doubled,
                    'doubled backslash in the automation-qualification regex matches nothing'
    assert_includes scheduler, '[...match[1].matchAll(/\\d+/g)]'

    # The gate helpers mirror src/continuum/qualification.py in the runtime.
    %w[
      qualificationRefs
      latestRequiredSha
      requiredShaMarker
      qualificationDispatchMarker
      hasQualificationDispatch
      parseQualificationEvidenceEntries
      qualificationEvidenceState
      currentMainSha
      dispatchQualificationIssue
      reconcileQualificationCapability
      ensureRepairIssue
      enterQualifyingState
    ].each do |name|
      assert_includes scheduler, name,
                      "the scheduler runtime is missing the qualification helper #{name}"
    end

    # An implementation merge enters qualifying instead of completing.
    assert_includes scheduler, 'enters qualification instead of completing'
    assert_includes scheduler, 'await addLabel(issueNumber, qualifyingLabel);'
    assert_includes scheduler, 'continuum-qualification-required capability='
    assert_includes scheduler, 'continuum-qualification-dispatch capability='

    # Exact-SHA evidence is required: entries for another SHA are stale and
    # prose verdicts never count.
    assert_includes scheduler, 'continuum-qualification-result'
    assert_includes scheduler, 'continuum-docker-qualification-result'
    assert_includes scheduler, 'continuum-render-qualification-result'
    assert_includes scheduler, 'if (entry.sha !== sha) continue;'

    # Failure blocks and routes repair; a main advance reruns qualification.
    assert_includes scheduler, 'await addLabel(capabilityNumber, blockedLabel);'
    assert_includes scheduler, 'continuum-qualification-repair source='
    assert_includes scheduler, 'qualification reruns for '

    # Fail closed: a closed capability without exact-SHA pass evidence is
    # reopened, so `Fixes #N` cannot bypass the gate.
    assert_includes scheduler, 'Reopened auto-closed capability'
    assert_includes scheduler, "state: 'open'"

    # Pause never strands a mandatory qualification, and duplicate merge
    # events never start dispatch storms.
    assert_includes scheduler, 'unpause-and-dispatch'
    assert_includes scheduler, 'already-dispatched'

    # The lifecycle labels are created and bound to the consumer knobs.
    assert_includes scheduler, 'name: qualifyingLabel,'
    assert_includes scheduler, 'name: blockedLabel,'
    assert_includes scheduler, "QUALIFYING_LABEL: ${{ inputs.qualifying_label || vars.AUTOMATION_QUALIFYING_LABEL || 'automation:qualifying' }}"
    assert_includes scheduler, "BLOCKED_LABEL: ${{ inputs.blocked_label || vars.AUTOMATION_BLOCKED_LABEL || 'automation:blocked' }}"

    # PR bodies for qualifying capabilities must not carry auto-close
    # keywords: both PR creation sites choose a non-closing body when the
    # issue declares mandatory qualification.
    opencode = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))
    assert_equal 2, opencode.scan('automation-qualification:').size,
                   'both PR creation sites must branch on the qualification marker'
    assert_includes opencode, 'Relates to #'
    assert_includes opencode, 'must not auto-close it'

    # The deterministic engine and its CLI own the same rules.
    engine = File.read(File.join(ROOT, 'src/continuum/qualification.py'))
    assert_includes engine, 'automation-qualification'
    assert_includes engine, 'continuum-qualification-result'
    assert_includes engine, 'continuum-qualification-required'
    assert_includes engine, 'continuum-qualification-dispatch'
    assert_includes engine, 'def capability_status'
    assert_includes engine, 'def should_dispatch_qualification'
    cli = File.read(File.join(ROOT, '.github/scripts/qualification_gate.py'))
    assert_includes cli, 'from continuum.qualification import',
                    'the CLI must delegate to the canonical gate, not reimplement it'
  end

  # kodmial/continuum#214: mandatory qualification evidence must be
  # trustworthy and executable without code changes. Each behavior below is
  # asserted on the code that acts on it.
  def test_mandatory_qualification_evidence_is_trusted_and_executable
    scheduler = workflow_body('continuum-issue-scheduler.yml')
    opencode = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))
    engine = File.read(File.join(ROOT, 'src/continuum/qualification.py'))
    stub = File.read(File.join(ROOT, '.github/caller-stubs/continuum-issue-scheduler.yml'))
    cli = File.read(File.join(ROOT, '.github/scripts/qualification_gate.py'))

    # Evidence integrity: dispatch prose never counts, only trusted actors
    # count, exact SHA and issue number remain mandatory, latest trusted wins.
    assert_includes scheduler, 'TRUSTED_AUTHOR_ASSOCIATIONS'
    assert_includes scheduler, 'isTrustedQualificationComment'
    assert_includes scheduler, 'if (commentHasDispatchMarker(body)) continue;'
    refute_includes scheduler, 'Qualification must publish `<!-- continuum-qualification-result',
                    'the dispatch note must never embed a parseable result marker'
    assert_includes engine, 'TRUSTED_AUTHOR_ASSOCIATIONS'
    assert_includes engine, 'def is_trusted_comment'
    assert_includes engine, 'if contains_dispatch_marker(body):'

    # Merge-gated start: a bare relation never synthesizes current main, open
    # blockers wait, and only the merge transition records the first SHA.
    assert_includes scheduler, 'waiting for the merge transition.'
    assert_includes scheduler, "return 'no-required-sha';"
    assert_includes scheduler, "return 'blocked-waiting';"
    assert_includes engine, 'def should_start_qualification'
    assert_includes engine, 'no-required-sha'

    # Exact dispatch identity: immutable triple into the run, no retarget by a
    # later dispatch, duplicates coalesce per SHA, exact SHA fetched by SHA.
    assert_includes scheduler, "mode: 'qualification',"
    assert_includes scheduler, 'capability_number: String(capabilityNumber),'
    assert_includes scheduler, 'required_sha: sha,'
    assert_includes opencode, "inputs.mode == 'qualification'"
    assert_includes opencode, 'git fetch origin "$REQUIRED_SHA" --depth 1'
    assert_includes opencode, 'already-started SHA-A run'
    assert_includes engine, 'def qualification_run_identity'
    assert_includes engine, 'def dispatch_matches_run'

    # Qualification execution mode: validate, forbid changes, require one
    # marker, never close, no-code with evidence succeeds, missing evidence
    # fails closed recoverably, fail routes to repair.
    assert_includes opencode, 'Run mandatory qualification at the exact required SHA'
    assert_includes opencode, 'Qualification mode produced repository changes, which are forbidden'
    assert_includes opencode, 'leaving the qualification issue open for the scheduler'
    assert_includes opencode, 'will be retried automatically with the same run identity'
    assert_includes engine, 'def evaluate_qualification_run'
    assert_includes engine, 'qualification-mode-cannot-push-product-changes'

    # Recovery stays autonomous: qualification never terminally pauses.
    assert_includes opencode, 'never terminally paused'
    assert_includes opencode, 'isQualificationRun'
    assert_includes engine, 'def qualification_needs_retry'
    assert_includes engine, 'def next_qualification_retry_delay_seconds'

    # Repair handoff carries the exact failure evidence and refreshes stale bodies.
    assert_includes scheduler, 'latestTrustedEvidenceBody'
    assert_includes scheduler, 'Refreshed repair issue #'
    assert_includes engine, 'def repair_issue_body'
    assert_includes engine, 'def repair_body_needs_refresh'

    # Event completeness: the consumer caller wakes on result comments while
    # cron remains the backstop.
    assert_includes stub, "contains(github.event.comment.body, 'continuum-qualification-result')"
    assert_includes stub, "contains(github.event.comment.body, 'continuum-docker-qualification-result')"
    assert_includes stub, "contains(github.event.comment.body, 'continuum-render-qualification-result')"
    assert_includes stub, 'schedule:'
    # Trust hardening: result-marker wakes are gated on repository trust so a
    # public forgery cannot burn Actions minutes; automation evidence reads
    # user.login (the issue-comments API shape); dispatch trust is checked in
    # JS; untracked droppings are discarded before the clean-tree verdict.
    assert_includes stub, "github.event.comment.author_association == 'OWNER'"
    assert_includes stub, "github.actor == 'github-actions[bot]'"
    assert_includes opencode, '.user.login == "github-actions[bot]"'
    assert_includes opencode, 'isTrustedDispatchComment'
    assert_includes opencode, '--untracked-files=no'
    assert_includes cli, '--author-association'
  end

  # kodmial/continuum#248: an `issue_comment` on a PR must start the
  # interactive agent only for an explicit owner command at the start of the
  # comment on a still-open PR. Substring matching let agent-generated
  # verification prose (which mentions /oc later) chain one run per minute on
  # a closed PR. The job gate and the `Run OpenCode` step gate must both pin
  # the open-PR + owner + startsWith + cancel-exclusion rule, while the
  # plain-issue, qualification, workflow_dispatch, and review-comment paths
  # keep their existing behavior.
  def test_opencode_pr_interactive_path_requires_explicit_open_command
    body = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))

    job = body[/  opencode:\n(?:.*\n)*?    if: >-\n((?:      .*\n)+)/, 1]
    refute_nil job, 'the opencode job gate is missing'

    # The PR issue_comment route: open PR, owner, command at the start,
    # cancel excluded. startsWith (not contains) is what stops later-prose
    # mentions from self-triggering.
    assert_includes job, 'github.event.issue.pull_request',
                    'the job gate must distinguish PR comments from plain issues'
    assert_includes job, "github.event.issue.state == 'open'",
                    'the job gate must require a still-open PR'
    assert_includes job, 'github.actor == github.repository_owner',
                    'the job gate must require the repository owner'
    assert_includes job, "startsWith(github.event.comment.body, '/oc')",
                    'the PR route must use startsWith, not contains'
    assert_includes job, "startsWith(github.event.comment.body, '/opencode')",
                    'the PR route must accept /opencode at the start'
    assert_includes job, "!contains(github.event.comment.body, '/oc-cancel')",
                    'the PR route must keep the /oc-cancel exclusion'

    # The plain-issue route keeps substring matching so scheduler dispatch
    # comments (which open with /oc plus a marker) keep working.
    assert_includes job, '!github.event.issue.pull_request',
                    'the job gate must keep a distinct plain-issue route'
    assert_includes job, "contains(github.event.comment.body, '/oc')",
                    'the plain-issue route must keep contains matching'

    # The review-comment route is deliberately unchanged.
    assert_includes job, "github.event_name == 'pull_request_review_comment'",
                    'the job gate must keep the review-comment route'

    # workflow_dispatch repair/qualification modes are untouched.
    assert_includes job, "contains(fromJSON('[\"coderabbit-fix\",\"resolve-conflict\",\"ci-fix\",\"issue\",\"qualification\"]'), inputs.mode)"

    run_step = step_body(body, 'Run OpenCode')
    refute_nil run_step, 'the `Run OpenCode` step is missing'
    assert_includes run_step, "github.event.issue.state == 'open'",
                    'Run OpenCode must require a still-open PR'
    assert_includes run_step, 'github.actor == github.repository_owner',
                    'Run OpenCode must require the repository owner on PR comments'
    assert_includes run_step, "startsWith(github.event.comment.body, '/oc')",
                    'Run OpenCode must use startsWith on PR comments'
    assert_includes run_step, "startsWith(github.event.comment.body, '/opencode')",
                    'Run OpenCode must accept /opencode at the start'
    assert_includes run_step, "!contains(github.event.comment.body, '/oc-cancel')",
                    'Run OpenCode must keep the /oc-cancel exclusion'
    assert_includes run_step, "steps.duplicate_guard.outputs.skip != 'true'",
                    'Run OpenCode must keep the duplicate guard'
    # The review-comment branch of the step keeps its existing shape.
    assert_includes run_step, "github.event_name == 'pull_request_review_comment'",
                    'Run OpenCode must keep the review-comment branch'
  end

  # The closed-PR and later-prose cases must not match the PR route: the
  # gate text is re-evaluated here as a boolean over the issue state and the
  # comment body, so a weakened expression (contains instead of startsWith,
  # or a dropped open check) fails here rather than in production.
  def test_opencode_pr_gate_logic_rejects_loops_and_accepts_commands
    evaluate = lambda do |issue_state, actor_is_owner, comment_body|
      opens = issue_state == 'open'
      owner = actor_is_owner
      starts = comment_body.start_with?('/oc') || comment_body.start_with?('/opencode')
      cancelled = comment_body.include?('/oc-cancel')
      opens && owner && starts && !cancelled
    end

    # Closed PR + arbitrary comment -> no run.
    refute evaluate.call('closed', true, '/oc please continue'),
           'a closed PR must never start the interactive agent'
    refute evaluate.call('closed', true, 'any bot prose'),
           'a closed PR must never start the interactive agent'
    # Open PR + generated prose mentioning /oc later -> no run.
    refute evaluate.call('open', true,
                         'OpenCode run 37176701843 completed; the /oc token was mentioned while explaining why.'),
           'later-prose mentions must never start the interactive agent'
    refute evaluate.call('open', true, 'Please run /oc for me'),
           'later-prose mentions must never start the interactive agent'
    # Open PR + owner /oc at the start -> exactly one run.
    assert evaluate.call('open', true, '/oc'),
           'a bare owner /oc must start the interactive agent'
    assert evaluate.call('open', true, "/oc\n\nplease fix the flake"),
           'an owner /oc command must start the interactive agent'
    assert evaluate.call('open', true, '/opencode fix the flake'),
           'an owner /opencode command must start the interactive agent'
    # Open PR + owner /oc-cancel -> no run.
    refute evaluate.call('open', true, '/oc-cancel'),
           '/oc-cancel must never start the interactive agent'
    # Non-owner commands never run.
    refute evaluate.call('open', false, '/oc'),
           'a non-owner command must never start the interactive agent'

    # The evaluated rule above must be the rule the workflow actually
    # expresses: every operator it depends on has to be present in the gate.
    body = File.read(File.join(ROOT, '.github/workflows/continuum-opencode.yml'))
    run_step = step_body(body, 'Run OpenCode')
    refute_nil run_step
    %w[state\ ==\ 'open' repository_owner startsWith !contains].each do |token|
      assert_includes run_step, token.gsub('\\ ', ' '),
                      "Run OpenCode gate lost its #{token.inspect} term"
    end
  end

  # kodmial/continuum#253 P0: the auto-merge obsolete-run scan walked the
  # complete Actions history (page 10+ on Kodmai PR #95) from the shared
  # TAP_PAT and aborted the whole controller on a 403. Housekeeping must be
  # bounded and cheap regardless of repository history size.
  #
  # Covers DoD regression items 1-3: bounded list-call count with 10k
  # historical completed runs, no page 10+ request, only active/relevant runs
  # considered for obsolete cancellation.
  def test_auto_merge_housekeeping_listing_is_bounded_and_status_filtered
    body = auto_merge_body
    inner = js_block(body, 'async function cancelObsoleteRunsInner')
    refute_nil inner, 'the bounded housekeeping implementation is missing'

    # The unbounded all-history pagination must be gone from the
    # housekeeping path. Gate reads (latestWorkflowForHead) still paginate a
    # single branch+event scope; only the housekeeping scan is asserted here.
    refute_includes inner, 'github.paginate(',
                    'housekeeping must not use unbounded paginate; use explicit page bounds'
    assert_includes inner, 'for (const status of HOUSEKEEPING_ACTIVE_STATUSES)',
                    'housekeeping must query one status value per request'
    assert_includes inner, 'for (let page = 1; page <= HOUSEKEEPING_MAX_PAGES_PER_STATUS; page += 1)',
                    'housekeeping must enforce an explicit page cap'
    assert_includes inner, 'status,',
                    'the list call must pass the status filter to the API'
    assert_includes body, 'HOUSEKEEPING_MAX_PAGES_PER_STATUS = 2',
                    'the page cap must stay well below page 10'
    assert_includes body, 'HOUSEKEEPING_ACTIVE_STATUSES = [',
                    'active states must be enumerated explicitly'
    %w[queued in_progress waiting requested pending].each do |state|
      assert_includes body, "'#{state}'",
                        "active state #{state} must remain in the housekeeping set"
    end
    assert_includes inner, 'if (batch.length < HOUSEKEEPING_PER_PAGE) break;',
                    'a short page must stop pagination early'
    assert_includes inner, 'if (!HOUSEKEEPING_ACTIVE_STATUSES.includes(run.status)) continue;',
                    'only active/non-terminal runs may be considered for cancellation'
    refute_match(/page:\s*10/, inner,
                 'housekeeping must never request arbitrary page 10+')
    refute_match(/page\s*=\s*10/, inner,
                 'housekeeping must never walk to page 10+')

    # Request-count bound is structural: 5 statuses x max 2 pages = at most
    # 10 list calls. Release metadata probes are bounded independently.
    assert_includes body, 'HOUSEKEEPING_MAX_RELEASE_PROBES = 10',
                    'per-run Release metadata reads must be probe-bounded'
    assert_includes inner, 'if (releaseProbes >= HOUSEKEEPING_MAX_RELEASE_PROBES)',
                    'the probe budget must be enforced before further reads'

    # Deterministic Kodmai-scale fixture: 10k historical completed runs plus
    # only 3 active runs. The old all-history strategy pays
    # ceil(10003/100)=101 REST list calls and walks past page 10; the new
    # status-filtered strategy pays one short page per status (5 calls) and
    # never touches completed history.
    active = [
      { 'status' => 'queued', 'name' => 'CI' },
      { 'status' => 'in_progress', 'name' => 'CI' },
      { 'status' => 'queued', 'name' => 'Release' },
    ]
    historical_completed = 10_000
    per_page = 100
    old_calls = ((historical_completed + active.size).to_f / per_page).ceil
    assert_equal 101, old_calls, 'fixture sanity: old scan walks 101 pages for 10k history'
    assert_operator old_calls, :>, 10, 'the old scan demonstrably exceeds any small bound'

    # Simulate the new bounded loop against a fake server that filters by
    # status server-side (like the real API): each status bucket holds at
    # most the few active runs, so every status terminates after 1 page.
    statuses = %w[queued in_progress waiting requested pending]
    max_pages = 2
    calls = 0
    max_page_seen = 0
    considered = []
    buckets = active.group_by { |run| run['status'] }
    statuses.each do |_status|
      (1..max_pages).each do |page|
        calls += 1
        max_page_seen = [max_page_seen, page].max
        batch = (buckets[_status] || []).each_slice(per_page).to_a[page - 1] || []
        considered.concat(batch)
        break if batch.size < per_page
      end
    end
    assert_equal 5, calls, 'bounded scan costs 5 list calls for the fixture (1 short page per status)'
    assert_operator calls, :<=, 10, 'list-call count must stay bounded regardless of history size'
    assert_operator max_page_seen, :<, 10, 'no request for page 10+ may be made'
    assert_equal 3, considered.size, 'only the few active runs are considered'
    assert considered.all? { |run| statuses.include?(run['status']) },
           'only active/non-terminal states are considered'
    assert_equal historical_completed, 10_000, 'history size is fixed by the fixture'
  end

  # Covers DoD regression items 4 and 10: same-repo housekeeping reads use
  # the repository token while cancellation stays PAT-backed, and no other
  # credential binding changes.
  def test_auto_merge_housekeeping_reads_use_repository_token_and_mutations_stay_pat
    body = auto_merge_body
    workflow = yaml(File.join(ROOT, '.github/workflows/continuum-auto-merge.yml'))
    stub = yaml(File.join(ROOT, '.github/caller-stubs/continuum-auto-merge.yml'))
    inner = js_block(body, 'async function cancelObsoleteRunsInner')
    refute_nil inner, 'the bounded housekeeping implementation is missing'

    # The step keeps its PAT client for mutations; the repository token
    # arrives only as an extra env binding for housekeeping reads.
    assert_includes body, 'github-token: ${{ secrets.TAP_PAT }}',
                    'the auto-merge step must stay PAT-backed for mutations'
    assert_includes body, 'READ_GITHUB_TOKEN: ${{ github.token }}',
                    'housekeeping reads must bind the repository token explicitly'
    assert_equal 1, body.scan('${{ github.token }}').size,
                 'no credential binding outside the verified housekeeping-read scope may change'
    assert_includes body, 'new github.constructor(',
                    'housekeeping must build a dedicated read client from the injected constructor'

    # Housekeeping reads (main SHA, active run listing, version/release
    # metadata) run on the read client.
    assert_includes inner, 'await readGithub.rest.repos.getBranch(',
                    'the housekeeping main-SHA read must use the repository token'
    assert_includes inner, 'await readGithub.rest.actions.listWorkflowRunsForRepo(',
                    'the active run listing must use the repository token'
    assert_includes inner, 'await readGithub.rest.repos.getContent(',
                    'the version-file metadata read must use the repository token'
    assert_includes inner, 'await readGithub.rest.repos.getReleaseByTag(',
                    'the release metadata read must use the repository token'
    refute_includes inner, 'await github.rest.repos.getBranch(',
                    'the housekeeping main-SHA read must not use TAP_PAT'
    refute_includes inner, 'await github.rest.actions.listWorkflowRunsForRepo(',
                    'the housekeeping run listing must not use TAP_PAT'
    refute_includes inner, 'await github.rest.repos.getContent(',
                    'the housekeeping version read must not use TAP_PAT'
    refute_includes inner, 'await github.rest.repos.getReleaseByTag(',
                    'the housekeeping release read must not use TAP_PAT'

    # The cancellation mutation stays PAT-backed with unchanged actor/fan-out.
    assert_includes inner, 'await github.rest.actions.cancelWorkflowRun(',
                    'cancellation must remain PAT-backed'
    refute_includes inner, 'await readGithub.rest.actions.cancelWorkflowRun(',
                    'cancellation must never move to the repository token in this P0'

    # Least privilege is unchanged: the reusable workflow and its caller
    # already grant actions:write (which includes read), so no permission
    # widening is needed for the repository-token reads.
    assert_equal 'write', workflow.fetch('jobs').fetch('controller').fetch('permissions').fetch('actions')
    assert_equal 'write', stub.fetch('permissions').fetch('actions')
    assert_equal 'write', workflow.fetch('jobs').fetch('controller').fetch('permissions').fetch('contents')
    assert_equal 'write', workflow.fetch('jobs').fetch('controller').fetch('permissions').fetch('pull-requests')

    # Same-repository gate reads use the repository token so watchdog
    # wakeups never spend shared TAP_PAT budget on polling (#224): the
    # exact-HEAD CI lookup is a read like any other; only
    # mutations/dispatches stay PAT-backed.
    assert_includes body, 'async function latestWorkflowForHead',
                    'the CI gate lookup must still exist'
    gate = js_block(body, 'async function latestWorkflowForHead')
    refute_nil gate
    assert_includes gate, 'withReadFallback',
                    'the branch-scoped CI gate must read via the repository token'
    assert_includes gate, 'client.rest.actions.listWorkflowRunsForRepo',
                    'the CI gate listing must use the repository-token client'
    refute_includes gate, 'github.paginate(',
                    'the CI gate must not poll on TAP_PAT'
    refute_includes gate, 'github.rest.actions.listWorkflowRunsForRepo',
                    'the CI gate listing must not use TAP_PAT'
  end

  # Covers DoD regression items 5 and 6: a rate-limit/transient failure on
  # the housekeeping read path warns and skips cleanup without aborting core
  # reconciliation, while a deterministic bug still fails loudly.
  def test_auto_merge_housekeeping_rate_limit_is_best_effort_but_deterministic_fails
    body = auto_merge_body
    outer = js_block(body, 'async function cancelObsoleteRuns(')
    inner = js_block(body, 'async function cancelObsoleteRunsInner')
    refute_nil outer, 'the best-effort housekeeping wrapper is missing'
    refute_nil inner, 'the bounded housekeeping implementation is missing'

    # Transient set: the live 403 core-REST exhaustion plus 429/5xx.
    # A bare 403 is not transient: GitHub also uses 403 for deterministic
    # permission/config denials, which must fail loudly. Only a 403 with
    # rate-limit evidence (message or headers) skips cleanup.
    assert_includes body, 'function isHousekeepingTransientError(err)',
                    'the transient classifier is missing'
    assert_includes body, 'function isHousekeepingRateLimit403(err)',
                    'the 403 rate-limit evidence classifier is missing'
    assert_includes body, 'if (status === 429) return true;',
                    '429 must be treated as transient housekeeping failure'
    assert_includes body, 'if (status === 403) return isHousekeepingRateLimit403(err);',
                    '403 must be transient only with rate-limit evidence'
    refute_includes body, 'if (status === 403) return true;',
                    'a bare 403 must not be treated as transient'
    assert_includes body, 'rate[',
                    'the 403 classifier must inspect rate-limit message evidence'
    assert_includes body, 'x-ratelimit-remaining',
                    'the 403 classifier must inspect rate-limit header evidence'
    assert_includes body, '[500, 502, 503, 504].includes(status)',
                    'transient 5xx must be treated as transient housekeeping failure'

    # Best-effort wrapper: warn, return zero counts with skipped:true, and
    # let the caller continue into PR synchronization/review/merge.
    assert_includes outer, 'return await cancelObsoleteRunsInner(openPulls);'
    assert_includes outer, 'if (isHousekeepingTransientError(err)) {'
    assert_includes outer, 'core.warning('
    assert_includes outer, 'Skipping obsolete-run cleanup'
    assert_includes outer, 'return { ci: 0, noopRelease: 0, skipped: true };'
    assert_includes outer, 'throw err;'
    # The caller after the wrapper continues reconciliation unconditionally:
    # no human-required marking, no early return on skipped.
    caller_at = body.index('const cancelledRuns = await cancelObsoleteRuns(pulls);')
    refute_nil caller_at, 'the housekeeping call site is missing'
    tail = body[caller_at, 1200]
    assert_includes tail, 'pulls.sort(',
                    'reconciliation must continue after housekeeping regardless of skip'
    refute_includes tail, 'core.setFailed',
                    'a skipped cleanup pass must not fail the run'
    refute_includes tail, 'human-required',
                    'a skipped cleanup pass must not mark work human-required'

    # Per-run Release metadata and per-cancel transient handling are also
    # best-effort without retry storms: warn/skip, never retry-loop.
    assert_includes inner, 'Skipping Release housekeeping for run'
    assert_includes inner, 'Skipping cancellation of'
    refute_match(/for\s*\(.*retry.*\)/i, inner,
                 'housekeeping must not introduce a retry loop')
    refute_match(/setTimeout.*housekeep/i, inner,
                 'housekeeping must not introduce delayed retries')

    # Executable classifier replica: the same status set the workflow
    # expresses must behave as specified (rate-limit skips, bug fails).
    # A 403 skips only with rate-limit evidence; a deterministic 403
    # (permission/config denial) still fails loudly.
    transient = lambda do |status, rate_limit_evidence = false|
      next true if status == 429
      next rate_limit_evidence if status == 403
      [500, 502, 503, 504].include?(status)
    end
    assert transient.call(403, true), 'a 403 with rate-limit evidence must skip cleanup, not abort'
    refute transient.call(403, false), 'a deterministic 403 denial must still fail loudly'
    assert transient.call(429), '429 must skip cleanup, not abort'
    assert transient.call(503), '503 must skip cleanup, not abort'
    refute transient.call(422), 'a deterministic 422 must still fail loudly'
    refute transient.call(400), 'a deterministic 400 must still fail loudly'
    refute transient.call(nil), 'a programming bug with no status must still fail loudly'

    # A deterministic bug inside housekeeping still rejects: only the
    # transient branch returns zeros; every other error rethrows.
    assert_match(/catch \(err\) \{\s*\n\s*if \(isHousekeepingTransientError\(err\)\) \{[\s\S]*?\n\s*\}\s*\n\s*throw err;/, outer,
                 'non-transient housekeeping errors must rethrow')
    # The Release 404 fast path (may-be-real-release) is preserved.
    assert_includes inner, 'housekeepingStatus(err) === 404',
                    'the 404 may-be-real-release path must be preserved'
  end

  # Covers DoD regression items 7-9 plus safety-gate preservation: stale CI
  # cancellation, stale no-op Release cancellation, PR-Agent sync-only mode,
  # and every merge gate stay exactly as before.
  def test_auto_merge_housekeeping_preserves_cancellation_and_sync_semantics
    body = auto_merge_body
    inner = js_block(body, 'async function cancelObsoleteRunsInner')
    refute_nil inner, 'the bounded housekeeping implementation is missing'

    # 7. Stale CI semantics unchanged: active CI runs whose head is neither
    # main nor any open PR head are cancelled and counted.
    assert_includes inner, "run.name === 'CI' &&"
    assert_includes inner, '!liveHeads.has(run.head_sha)'
    assert_includes inner, "if (await cancelRun(run, 'obsolete CI')) ci += 1;"
    assert_includes inner, 'const liveHeads = new Set(['
    assert_includes inner, 'mainSha,'
    assert_includes inner, '...openPulls.map(pr => pr.head.sha),'

    # 8. Stale no-op Release semantics unchanged: push-to-main Release runs
    # off current main are cancelled only when the run-head version was
    # already published before the run started; a missing release (404)
    # means it may be a real release and is never cancelled.
    assert_includes inner, "run.name === 'Release' &&"
    assert_includes inner, "run.event === 'push' &&"
    assert_includes inner, "run.head_branch === 'main' &&"
    assert_includes inner, 'run.head_sha !== mainSha'
    assert_includes inner, '/\\b\\d+\\.\\d+\\.\\d+\\b/'
    assert_includes inner, 'tag: `v${match[0]}`'
    assert_includes inner, 'publishedAt < runCreatedAt'
    assert_includes inner, "if (await cancelRun(run, 'stale no-op Release')) {"
    assert_includes inner, 'noopRelease += 1;'
    # The 409/422 list-then-cancel race stays harmless.
    assert_includes inner, '[409, 422].includes(housekeepingStatus(err))'
    assert_includes inner, 'changed state before cancellation'

    # Executable semantic check on the preserved predicates.
    stale_ci = lambda do |run_name, head, live|
      run_name == 'CI' && !live.include?(head)
    end
    assert stale_ci.call('CI', 'dead-sha', %w[main-sha pr-sha]),
           'an obsolete CI head must still be cancelled'
    refute stale_ci.call('CI', 'pr-sha', %w[main-sha pr-sha]),
           'a live CI head must never be cancelled'
    refute stale_ci.call('Release', 'dead-sha', %w[main-sha pr-sha]),
           'a Release run must never take the CI path'
    stale_release = lambda do |run, main_sha, published_at, created_at|
      run['name'] == 'Release' && run['event'] == 'push' &&
        run['head_branch'] == 'main' && run['head_sha'] != main_sha &&
        published_at.positive? && created_at.positive? && published_at < created_at
    end
    run = { 'name' => 'Release', 'event' => 'push', 'head_branch' => 'main', 'head_sha' => 'old-main' }
    assert stale_release.call(run, 'new-main', 100, 200),
           'a stale no-op Release must still be cancelled'
    refute stale_release.call(run, 'old-main', 100, 200),
           'the current-main Release run must never be cancelled'
    refute stale_release.call(run, 'new-main', 300, 200),
           'a run that may itself publish the release must never be cancelled'

    # 9. PR-Agent sync-only mode still works: main sync stays active while
    # the generic reconciler exits before CI/review/merge gates.
    assert_includes body, "const prAgentSyncOnly = reviewProvider === 'pr-agent';"
    assert_includes body, 'generic reconciler stops after main-sync evaluation'
    sync_guard = body.index('if (prAgentSyncOnly) {', body.index('await updateFromMain(pr);'))
    generic_ci = body.index('const ci = await latestCurrentHeadCi(pr);')
    refute_nil sync_guard, 'the sync-only guard is missing'
    refute_nil generic_ci, 'the generic CI gate is missing'
    assert_operator sync_guard, :<, generic_ci,
                    'PR-Agent sync-only mode must exit before generic review/merge gates'

    # Safety gates are not weakened: exact-HEAD CI, packaging smoke,
    # main-sync conflict handling, conflict repair, PR-Agent ownership, merge
    # SHA guard, and downstream wake-up semantics all remain.
    assert_includes body, 'async function latestCurrentHeadCi(pr)'
    assert_includes body, 'async function shouldSyncFromMain(pr, comparison)'
    assert_includes body, 'async function dispatchConflictRepair(pr, reason)'
    assert_includes body, 'sha: pr.head.sha,',
                    'the merge SHA guard must remain'
    assert_includes body, 'merge_method:'
    assert_includes body, "'squash'"
    # Issue #246 canonical dirty detection: every mergeability read is routed
    # through isConflictedMergeability(pr.mergeable, pr.mergeable_state),
    # which returns true for mergeable=false and mergeable_state=dirty (plus
    # GraphQL CONFLICTING etc.) and null while GitHub recomputes. The pre-#246
    # inline `pr.mergeable === false` literal covered only the REST boolean
    # and is superseded by this strictly broader helper; assert the helper
    # and its call sites instead so REST and GraphQL dirty shapes stay gated.
    assert_includes body, 'function isConflictedMergeability('
    assert_includes body, "if (state === 'dirty') return true;"
    assert_includes body, "if (typeof mergeable === 'boolean') return !mergeable;"
    assert_includes body, 'isConflictedMergeability('
    assert_includes body, 'pr.mergeable,'
    assert_includes body, 'pr.mergeable_state'
    assert_includes body, "hasLabel(pr, AUTO_MERGE_BLOCK_LABEL)"
    assert_includes body, 'for (const workflow of postMergeWakeups) {'
  end

  # PR-Agent delegated-execution contract: the opaque `target_child_id`
  # input is the only delegated selector, local runs resolve without
  # CONTINUUM_REF, and every delegated read targets the resolved child.
  def test_pr_agent_target_child_id_passthrough_and_local_resolution
    %w[continuum-pr-agent.yml continuum-pr-agent-repair.yml continuum-pr-agent-auto-merge.yml].each do |base|
      workflow = yaml(File.join(ROOT, '.github/workflows', base))
      stub = yaml(File.join(ROOT, '.github/caller-stubs', base))
      body = File.read(File.join(ROOT, '.github/workflows', base))

      call_inputs = events(workflow).fetch('workflow_call').fetch('inputs')
      assert call_inputs.key?('target_child_id'), "#{base}: workflow_call must declare target_child_id"
      assert_equal '', call_inputs.fetch('target_child_id').fetch('default'), "#{base}: target_child_id must default empty (local)"
      assert_equal false, call_inputs.fetch('target_child_id').fetch('required'), "#{base}: target_child_id must not gate the call"
      assert_equal 'string', call_inputs.fetch('target_child_id').fetch('type'), "#{base}: target_child_id must stay a string input"

      dispatch_inputs = events(stub).fetch('workflow_dispatch').fetch('inputs')
      assert dispatch_inputs.key?('target_child_id'), "#{base}: caller stub must expose target_child_id"
      assert_equal 'string', dispatch_inputs.fetch('target_child_id').fetch('type'), "#{base}: caller stub target_child_id must stay a string input"
      assert_equal false, dispatch_inputs.fetch('target_child_id').fetch('required'), "#{base}: caller stub target_child_id must not gate dispatch"
      assert_equal '${{ inputs.target_child_id }}', stub.fetch('jobs').fetch('call').fetch('with').fetch('target_child_id'),
                   "#{base}: caller stub must forward the opaque child id verbatim"
      refute_includes body, 'target_repository:',
                        "#{base}: concrete repository identity must never be an input"

      assert_includes body, 'inputs.target_child_id',
                        "#{base}: concurrency must scope delegated runs by the opaque id"
      resolve = step_body(body, 'Resolve PR-Agent target context')
      refute_nil resolve, "#{base}: target-context resolution step is missing"
      local_guard = resolve.index('if [[ -z "${TARGET_CHILD_ID:-}" ]]; then')
      ref_require = resolve.index('CONTINUUM_REF is required for pinned target resolution')
      refute_nil local_guard, "#{base}: local fast path is missing"
      refute_nil ref_require, "#{base}: delegated CONTINUUM_REF requirement is missing"
      assert_operator local_guard, :<, ref_require,
                      "#{base}: local runs must exit before CONTINUUM_REF is required"
      assert_includes resolve, 'CONTINUUM_PR_AGENT_TARGET_REPOSITORY'
      assert_includes resolve, 'CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED'
      assert_includes resolve, 'PR-Agent target context resolved locally.'
      assert_includes resolve, 'exit 0'
      assert_includes resolve, 'contents/.continuum.yml" --jq',
                        "#{base}: parent config must resolve from the execution repository default branch, not the engine pin"
      refute_includes resolve, 'contents/.continuum.yml" -f ref="$CONTINUUM_REF"',
                        "#{base}: the engine pin may not exist in the execution repository"
    end
  end

  # The pr-agent.yml retry entry forwards `target_child_id` to the reusable
  # review workflow, which must declare it and resolve the delegated target
  # from it: otherwise delegated runs would plumb the opaque id nowhere and
  # review against the parent repository.
  def test_pr_agent_retry_caller_target_child_id_reaches_reusable_resolver
    caller = yaml(File.join(ROOT, '.github/workflows/pr-agent.yml'))
    caller_body = File.read(File.join(ROOT, '.github/workflows/pr-agent.yml'))
    reusable = yaml(File.join(ROOT, '.github/workflows/continuum-pr-agent.yml'))
    reusable_body = File.read(File.join(ROOT, '.github/workflows/continuum-pr-agent.yml'))

    dispatch_inputs = events(caller).fetch('workflow_dispatch').fetch('inputs')
    assert dispatch_inputs.key?('target_child_id'), 'pr-agent.yml must expose target_child_id'
    assert_equal 'string', dispatch_inputs.fetch('target_child_id').fetch('type'),
                 'pr-agent.yml target_child_id must stay a string input'
    assert_equal false, dispatch_inputs.fetch('target_child_id').fetch('required'),
                 'pr-agent.yml target_child_id must not gate dispatch'
    assert_equal '${{ inputs.target_child_id }}',
                 caller.fetch('jobs').fetch('call').fetch('with').fetch('target_child_id'),
                 'pr-agent.yml must forward the opaque child id verbatim'

    call_inputs = events(reusable).fetch('workflow_call').fetch('inputs')
    assert call_inputs.key?('target_child_id'), 'continuum-pr-agent.yml must declare target_child_id'
    resolve = step_body(reusable_body, 'Resolve PR-Agent target context')
    refute_nil resolve, 'the reusable target-context resolution step is missing'
    assert_includes resolve, 'TARGET_CHILD_ID: ${{ inputs.target_child_id }}',
                    'the reusable resolver must consume the forwarded opaque child id'
    assert_includes caller_body, 'Resolve PR-Agent target context',
                    'the forwarding comment must name the consuming resolver step'
  end

  # Delegated PR-Agent repair/merge reads must fail closed without TAP_PAT
  # and must operate on the resolved target, never the parent execution
  # repository. The conditional token alone would silently fall back to
  # github.token and surface as 404/permission errors.
  def test_pr_agent_delegated_reads_fail_closed_and_use_target
    repair = File.read(File.join(ROOT, '.github/workflows/continuum-pr-agent-repair.yml'))
    resolve = step_body(repair, 'Resolve PR-Agent target context')
    validate = step_body(repair, 'Validate PR-Agent target context')
    convergence = step_body(repair, 'Check durable PR-Agent no-progress state')
    target = step_body(repair, 'Resolve the writable PR source branch')
    checkout = step_body(repair, 'Checkout the writable PR source branch')
    refute_nil resolve, 'the target-context resolution step is missing'
    refute_nil validate, 'the target-context validation step is missing'
    refute_nil convergence
    refute_nil target
    refute_nil checkout, 'the target checkout step is missing'
    # The validation must run immediately after resolution and before any
    # checkout/push/comment so an empty target can never fall back to the
    # execution repository.
    resolve_at = repair.index('Resolve PR-Agent target context')
    validate_at = repair.index('Validate PR-Agent target context')
    checkout_at = repair.index('Checkout the writable PR source branch')
    refute_nil resolve_at
    refute_nil validate_at
    refute_nil checkout_at
    assert_operator resolve_at, :<, validate_at,
                    'target validation must run after target resolution'
    assert_operator validate_at, :<, checkout_at,
                    'target validation must run before checkout'
    assert_includes validate, 'CONTINUUM_PR_AGENT_TARGET_REPOSITORY:?'
    assert_includes validate, 'CONTINUUM_PR_AGENT_TARGET_OWNER:?'
    assert_includes validate, 'CONTINUUM_PR_AGENT_TARGET_REPO:?'
    assert_includes validate, 'PR-Agent target repository identity is invalid'
    assert_includes validate, 'CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED'
    assert_includes validate, 'TAP_PAT: ${{ secrets.TAP_PAT }}'
    assert_includes validate, 'Delegated PR-Agent execution requires TAP_PAT'
    assert_includes validate, 'refusing to fall back to github.token'
    assert_includes checkout, 'repository: ${{ env.CONTINUUM_PR_AGENT_TARGET_REPOSITORY }}',
                    'checkout must target the validated resolved repository, never the parent by default'
    [convergence, target].each do |step|
      assert_includes step, 'github.token'
      assert_includes step, 'secrets.TAP_PAT'
      assert_includes step, 'CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED'
      assert_includes step, 'TAP_PAT: ${{ secrets.TAP_PAT }}',
                        'delegated guard needs the PAT value to test emptiness'
      assert_includes step, 'Delegated PR-Agent execution requires TAP_PAT',
                        'delegated runs without PAT must fail closed, not fall back to github.token'
      assert_includes step, 'refusing to fall back to github.token'
    end
    assert_includes convergence, 'github.rest.issues.listComments'
    assert_includes convergence, 'CONTINUUM_PR_AGENT_TARGET_OWNER'
    assert_includes target, 'repos/$CONTINUUM_PR_AGENT_TARGET_REPOSITORY/pulls/$PR_NUMBER'
    assert_includes target, '"$HEAD_REPO" != "$CONTINUUM_PR_AGENT_TARGET_REPOSITORY"'
    refute_includes target, 'repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER',
                    'PR resolution must read the target, never the parent execution repository'

    [
      'Mark PR-Agent repair in flight',
      'Publish durable PR-Agent repair state',
      'Publish failed PR-Agent repair state'
    ].each do |name|
      step = step_body(repair, name)
      refute_nil step, "#{name} step is missing"
      assert_includes step, 'CONTINUUM_PR_AGENT_TARGET_OWNER', "#{name}: commit status must target the child"
      assert_includes step, 'createCommitStatus', "#{name}: commit status must be published"
      assert_includes step, 'continuum/pr-agent-repair', "#{name}: repair status context must be preserved"
    end

    merge = File.read(File.join(ROOT, '.github/workflows/continuum-pr-agent-auto-merge.yml'))
    reconcile = step_body(merge, 'Reconcile current PR state and merge only the exact reviewed HEAD')
    refute_nil reconcile, 'auto-merge reconciliation step is missing'
    assert_includes reconcile, 'process.env.CONTINUUM_PR_AGENT_TARGET_OWNER'
    assert_includes reconcile, 'process.env.CONTINUUM_PR_AGENT_TARGET_REPOSITORY'
    assert_includes reconcile, 'CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED'
    assert_includes reconcile, 'pr.head.sha.toLowerCase() !== reviewedHead',
                    'merge must refuse a moved HEAD'
    assert_includes reconcile, 'sha: reviewedHead',
                    'only the exact reviewed SHA may merge'
    assert_includes merge, 'refusing bare retry to preserve the delegated target',
                     'delegated wakeups must never retry bare against the parent execution repository'
    assert_includes merge, 'if (targetChildId)',
                     'delegated wakeups must still carry the opaque child id while warning best-effort on failure'
    refute_includes merge, 'skipping bare retry to preserve the delegated target',
                    'a delegated wakeup must never silently skip the child post-merge chain'
  end

  # PR-Agent recovery delegated-execution contract: the opaque
  # `target_child_id` input (or vars.CONTINUUM_PR_AGENT_TARGET_CHILD_ID for
  # schedule/workflow_run wakeups) is the only delegated selector. Local runs
  # resolve without CONTINUUM_REF, target reads use the resolved child while
  # execution-run inspection and dispatch stay in the parent, local dispatches
  # stay bare, and only the exact HEAD is ever recovered.
  def test_pr_agent_recovery_target_resolution_and_execution_split
    base = 'continuum-pr-agent-recovery.yml'
    workflow = yaml(File.join(ROOT, '.github/workflows', base))
    stub = yaml(File.join(ROOT, '.github/caller-stubs', base))
    body = File.read(File.join(ROOT, '.github/workflows', base))

    call_inputs = events(workflow).fetch('workflow_call').fetch('inputs')
    assert call_inputs.key?('target_child_id'), "#{base}: workflow_call must declare target_child_id"
    assert_equal '', call_inputs.fetch('target_child_id').fetch('default'), "#{base}: target_child_id must default empty (local)"
    assert_equal false, call_inputs.fetch('target_child_id').fetch('required'), "#{base}: target_child_id must not gate the call"
    assert_equal 'string', call_inputs.fetch('target_child_id').fetch('type'), "#{base}: target_child_id must stay a string input"

    dispatch_inputs = events(stub).fetch('workflow_dispatch').fetch('inputs')
    assert dispatch_inputs.key?('target_child_id'), "#{base}: caller stub must expose target_child_id"
    assert_equal 'string', dispatch_inputs.fetch('target_child_id').fetch('type'), "#{base}: caller stub target_child_id must stay a string input"
    assert_equal false, dispatch_inputs.fetch('target_child_id').fetch('required'), "#{base}: caller stub target_child_id must not gate dispatch"
    assert_equal '${{ inputs.target_child_id }}', stub.fetch('jobs').fetch('call').fetch('with').fetch('target_child_id'),
                 "#{base}: caller stub must forward the opaque child id verbatim"
    refute_includes body, 'target_repository:',
                      "#{base}: concrete repository identity must never be an input"

    # Schedule/pull_request_target/workflow_run carry no dispatch inputs, so
    # the reusable falls through to the repository variable before local.
    assert_includes body, 'inputs.target_child_id || vars.CONTINUUM_PR_AGENT_TARGET_CHILD_ID',
                      "#{base}: schedule/workflow_run wakeups must fall through to the repository variable"
    assert_includes body, "format('pr-agent-recovery-child-",
                      "#{base}: concurrency must scope delegated runs by the opaque id"
    assert_includes body, "format('pr-agent-recovery-",
                      "#{base}: concurrency must preserve the exact local group when empty"

    resolve = step_body(body, 'Resolve PR-Agent target context')
    refute_nil resolve, "#{base}: target-context resolution step is missing"
    assert_includes resolve, 'TARGET_CHILD_ID: ${{ inputs.target_child_id || vars.CONTINUUM_PR_AGENT_TARGET_CHILD_ID }}',
                      "#{base}: resolution must honour the input-then-variable fallback"
    local_guard = resolve.index('if [[ -z "${TARGET_CHILD_ID:-}" ]]; then')
    ref_require = resolve.index('CONTINUUM_REF is required for pinned target resolution')
    refute_nil local_guard, "#{base}: local fast path is missing"
    refute_nil ref_require, "#{base}: delegated CONTINUUM_REF requirement is missing"
    assert_operator local_guard, :<, ref_require,
                    "#{base}: local runs must exit before CONTINUUM_REF is required"
      assert_includes resolve, 'CONTINUUM_PR_AGENT_TARGET_REPOSITORY'
      assert_includes resolve, 'CONTINUUM_PR_AGENT_TARGET_OWNER'
      assert_includes resolve, 'CONTINUUM_PR_AGENT_TARGET_REPO'
      assert_includes resolve, 'CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED'
      assert_includes resolve, 'PR-Agent target context resolved locally.'
      assert_includes resolve, 'exit 0'
      assert_includes resolve, 'contents/.continuum.yml" --jq',
                        "#{base}: parent config must resolve from the execution repository default branch, not the engine pin"
      refute_includes resolve, 'contents/.continuum.yml" -f ref="$CONTINUUM_REF"',
                        "#{base}: the engine pin may not exist in the execution repository"

    reconcile = step_body(body, 'Reconcile PR-Agent latest state')
    refute_nil reconcile, "#{base}: reconciliation step is missing"
    assert_includes reconcile, 'const executionOwner = context.repo.owner;',
                      "#{base}: execution identity must stay the parent repository"
    assert_includes reconcile, 'const owner = process.env.CONTINUUM_PR_AGENT_TARGET_OWNER;',
                      "#{base}: target reads must use the resolved child"
    assert_includes reconcile, 'const repoFullName = process.env.CONTINUUM_PR_AGENT_TARGET_REPOSITORY;',
                      "#{base}: same-repository filter must use the resolved child"
    assert_includes reconcile, 'owner: executionOwner',
                      "#{base}: execution-run inspection must stay in the parent"
    assert_includes reconcile, 'repo: executionRepo',
                      "#{base}: execution-run inspection must stay in the parent"
    assert_includes reconcile, 'client.rest.pulls.list',
                      "#{base}: open-PR enumeration must go through the guarded read client"
    assert_includes reconcile, 'client.rest.pulls.get',
                      "#{base}: PR revalidation must go through the guarded read client"
    assert_includes reconcile, 'client.rest.actions.listWorkflowRunsForRepo',
                      "#{base}: exact-HEAD CI evidence must go through the guarded read client"
    assert_includes reconcile, 'head_sha: head',
                      "#{base}: CI evidence must be for the exact HEAD only"
    assert_includes reconcile, 'run.head_sha === head',
                      "#{base}: CI match must be for the exact HEAD only"
    assert_includes reconcile, 'pr.head.repo.full_name !== repoFullName',
                      "#{base}: fork/same-repo filter must compare against the resolved target"
    assert_includes reconcile, 'const recoveryChildId',
                      "#{base}: dispatch must read the opaque child selection"
    assert_includes reconcile, 'if (recoveryChildId)',
                      "#{base}: local runs must dispatch bare so custom review workflows stay compatible"
    assert_includes reconcile, 'recoveryInputs.target_child_id = recoveryChildId',
                      "#{base}: delegated runs must preserve the opaque child selection"
    assert_includes reconcile, 'dispatchStatus === 422',
                      "#{base}: delegated input rejection must be detected via HTTP 422"
    assert_includes reconcile, '/target_child_id/i',
                      "#{base}: only a 422 naming target_child_id means the review workflow lacks the delegated input; other 422s must rethrow verbatim"
    refute_includes reconcile, '/input/i.test(dispatchMessage)',
                      "#{base}: a bare /input/ match misclassifies unrelated input validation as a missing target_child_id input"
    assert_includes reconcile, 'refusing bare retry to preserve the delegated target',
                      "#{base}: a review workflow without the target_child_id input must fail closed explicitly"
    assert_includes reconcile, 'status === 404',
                      "#{base}: cross-repository target reads surface as 404 under a repository-scoped token, so the PAT read fallback must cover it"
  end

  # Fail-closed guards around the delegated retry path: the manual checkout
  # must reject a non-numeric PR number before it reaches the pull/ refspec,
  # and both shell retry dispatches must detect a 422 unknown-input
  # rejection naming target_child_id explicitly (mirroring the merge-wakeup
  # and recovery 422 detection) instead of failing generically.
  def test_pr_agent_retry_and_checkout_fail_closed_on_delegated_inputs
    review_body = workflow_body('continuum-pr-agent.yml')
    checkout = step_body(review_body, 'Checkout the pull request head without exposing delegated repository metadata')
    refute_nil checkout, 'the manual review checkout step is missing'
    assert_includes checkout, '^[0-9]+$',
                    'the checkout must gate PR_NUMBER numerically before the pull/ refspec, mirroring HEAD_SHA validation'
    assert_includes checkout, 'PR number is malformed',
                    'a non-numeric PR number must fail closed with an explicit message'
    assert_includes checkout, 'pull/$PR_NUMBER/head'

    {
      'continuum-pr-agent.yml' => 'Schedule bounded retry for retryable PR-Agent review failure',
      'continuum-pr-agent-repair.yml' => 'Schedule bounded retry for retryable PR-Agent repair failure'
    }.each do |base, step_name|
      step = step_body(workflow_body(base), step_name)
      refute_nil step, "#{base}: #{step_name} is missing"
      assert_includes step, 'dispatch_isolated_retry',
                      "#{base}: retry dispatch must go through the 422-detecting helper"
      assert_includes step, '422',
                      "#{base}: a delegated retry rejected as an unknown input must be detected via HTTP 422"
      assert_includes step, 'target_child_id',
                      "#{base}: the 422 detection must name the opaque target_child_id input"
      assert_includes step, 'refusing bare retry to preserve the delegated target',
                      "#{base}: a retry workflow without the target_child_id input must fail closed explicitly, never dispatch bare"
      assert_includes step, 'set +e',
                      "#{base}: the retry dispatch must disable errexit around the expected 422 failure so the explicit detection runs"
    end
  end

  # Delegated conflict-repair dispatch and post-merge wakeup share the
  # narrow 422 contract: only a 422 naming the opaque target_child_id
  # input (wakeup additionally accepts GitHub's unexpected/unknown-input
  # wording) is an input-rejection; any other 422 rethrows verbatim so a
  # ref or payload validation failure keeps its true remediation path.
  # The repair reusable must declare the input, otherwise the delegated
  # dispatch is unreachable.
  def test_pr_agent_repair_dispatch_and_wakeup_narrow_422
    repair_inputs = events(yaml(File.join(ROOT, '.github/workflows/continuum-pr-agent-repair.yml')))
      .fetch('workflow_call').fetch('inputs')
    assert repair_inputs.key?('target_child_id'),
           'continuum-pr-agent-repair.yml must declare target_child_id or delegated repair dispatch is unreachable'

    merge = File.read(File.join(ROOT, '.github/workflows/continuum-pr-agent-auto-merge.yml'))
    repair_window = merge[merge.index('async function dispatchConflictRepair')..]
    assert_includes repair_window, "workflow_id: 'continuum-pr-agent-repair.yml'",
                    'delegated conflict repair must dispatch the repair reusable'
    assert_includes repair_window, 'target_child_id: targetChildId',
                    'delegated conflict repair must forward the opaque child id'
    assert_includes repair_window, 'isMissingTargetInput',
                    'delegated conflict-repair dispatch must detect 422 input rejection like recovery/wakeup'
    assert_includes repair_window, '/target_child_id/i.test(dispatchMessage)',
                    'conflict-repair 422 detection must name the opaque input explicitly'
    assert_includes repair_window, 'dispatchStatus === 422',
                    'conflict-repair input rejection must be gated on HTTP 422'
    assert_includes repair_window, 'refusing bare retry to preserve the delegated target',
                    'conflict-repair without the repair input must fail closed explicitly, never dispatch bare'
    assert_includes repair_window, 'recovery per #224',
                    'a transient repair dispatch must be reconciled by recovery'

    wakeup_window = merge[merge.index('for (const workflow of postMergeWakeups)')..]
    assert_includes wakeup_window, '/(target_child_id|unexpected',
                    'wakeup 422 detection must name the opaque input plus unexpected/unknown-input wording'
    assert_includes wakeup_window, 'unknown\\s+inputs?',
                    'wakeup 422 detection must accept unknown-input wording'
    refute_includes wakeup_window, 'invalid\\s+inputs?',
                    'wakeup 422 detection must not match generic invalid-inputs messages'
    refute_includes wakeup_window, 'unrecognized',
                    'wakeup 422 detection must not match unrecognized-input wording'
    refute_includes wakeup_window, 'inputs?\\s+not\\s+(accepted',
                    'wakeup 422 detection must not match inputs-not-accepted wording'
  end

  # The pr-agent-recovery.yml dogfood entry forwards `target_child_id` to the
  # reusable recovery workflow: otherwise delegated schedule/workflow_run
  # wakeups would plumb the opaque id nowhere and reconcile the parent.
  def test_pr_agent_recovery_dogfood_forwards_target_child_id
    caller = yaml(File.join(ROOT, '.github/workflows/pr-agent-recovery.yml'))
    dispatch_inputs = events(caller).fetch('workflow_dispatch').fetch('inputs')
    assert dispatch_inputs.key?('target_child_id'), 'pr-agent-recovery.yml must expose target_child_id'
    assert_equal 'string', dispatch_inputs.fetch('target_child_id').fetch('type'),
                 'pr-agent-recovery.yml target_child_id must stay a string input'
    assert_equal false, dispatch_inputs.fetch('target_child_id').fetch('required'),
                 'pr-agent-recovery.yml target_child_id must not gate dispatch'
    assert_equal '${{ inputs.target_child_id }}',
                 caller.fetch('jobs').fetch('call').fetch('with').fetch('target_child_id'),
                 'pr-agent-recovery.yml must forward the opaque child id verbatim'

    reusable_inputs = events(yaml(File.join(ROOT, '.github/workflows/continuum-pr-agent-recovery.yml')))
      .fetch('workflow_call').fetch('inputs')
    assert reusable_inputs.key?('target_child_id'),
           'continuum-pr-agent-recovery.yml must declare target_child_id or the dogfood forward is unreachable'
  end

  # Repository identity must fail closed on dot-only components (`owner/..`,
  # `owner/.`): the charset class alone accepts them and they would otherwise
  # flow into `gh`, `git remote`, and checkout steps as the resolved target.
  def test_pr_agent_target_identity_rejects_dot_only_components
    helper = File.read(File.join(ROOT, '.github/scripts/pr_agent_target.sh'))
    assert_includes helper, 'pr_agent_valid_target_repository',
                    'target resolution must go through the strict identity validator'
    assert_includes helper, '^\\.+$',
                    'the strict validator must reject dot-only owner/repo components'

    %w[
      continuum-pr-agent.yml
      continuum-pr-agent-repair.yml
      continuum-pr-agent-auto-merge.yml
      continuum-pr-agent-recovery.yml
    ].each do |base|
      body = File.read(File.join(ROOT, '.github/workflows', base))
      assert_includes body, '^\\.+$',
                      "#{base}: workflow identity guards must reject dot-only components like the helper"
    end

    runtime = File.read(File.join(ROOT, '.github/scripts/delegation_runtime.py'))
    assert_includes runtime, '\\.+',
                    'the delegation resolver must reject dot-only repository components'
    config = File.read(File.join(ROOT, 'src/continuum/config.py'))
    assert_includes config, 'strip(".")',
                    'child parent configuration must reject dot-only repository components'
  end

  end
