#!/usr/bin/env ruby
# frozen_string_literal: true

ROOT = File.expand_path('..', __dir__)

targets = [
  *Dir[File.join(ROOT, '.github/workflows/*.yml')],
  *Dir[File.join(ROOT, '.github/caller-stubs/**/*.yml')],
  *Dir[File.join(ROOT, '.github/scripts/*')].select { |path| File.file?(path) },
  *Dir[File.join(ROOT, 'src/**/*')].select { |path| File.file?(path) },
  File.join(ROOT, 'install.sh')
].uniq.sort

forbidden = {
  /nanodictate/i => 'NanoDictate product identifier',
  /runtime-lab/i => 'Runtime Lab product identifier',
  /\bkodmai\b/i => 'CodeMy product identifier',
  /kodmial\/kodmai\b/i => 'CodeMy repository identifier',
  /NANODICTATE_SIGNING/ => 'consumer-specific signing secret',
  /Sources\/NanoDictateCore/ => 'consumer-specific source layout',
  /homebrew-nanodictate|macports-nanodictate/i => 'consumer-specific distribution repository',
  /macos-15/ => 'hardcoded consumer runner',
  /runtime-worker-owned/ => 'consumer-specific legacy marker'
}.freeze

violations = []
targets.each do |path|
  File.readlines(path, chomp: true).each_with_index do |line, index|
    forbidden.each do |pattern, reason|
      next unless line.match?(pattern)
      violations << "#{path.delete_prefix(ROOT + '/') }:#{index + 1}: #{reason}: #{line.strip}"
    end
  end
end

if violations.any?
  warn 'Consumer-specific assumptions found in Continuum generic implementation/templates:'
  violations.each { |line| warn "  #{line}" }
  exit 1
end

puts "Project-agnostic guard passed for #{targets.size} implementation/template files."
