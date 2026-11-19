require "json"

User.current = User.find_by!(login: "admin")
tracker = Tracker.first
status_ids = IssueStatus.order(:id).pluck(:id)
priority = IssuePriority.default
raise "Redmine has no active tracker, status, or priority" unless tracker && status_ids.any? && priority

projects = 12.times.map do |index|
  project = Project.create!(
    name: "Latency study project #{index + 1}",
    identifier: "latency-study-#{index + 1}",
    description: "Synthetic project data for a database latency workload.",
    is_public: false,
    enabled_module_names: ["issue_tracking"]
  )
  project.trackers = [tracker]
  project
end

issues = 360.times.map do |index|
  issue = Issue.new(
    project: projects[index % projects.length],
    tracker: tracker,
    status_id: status_ids[index % status_ids.length],
    priority: priority,
    author: User.current,
    subject: "Synthetic issue #{index + 1}",
    description: "Issue description with stable seeded content #{index + 1}."
  )
  issue.init_journal(User.current, "Synthetic update #{index + 1}.")
  issue.save!
  issue
end

puts JSON.generate(projects: projects.length, issues: issues.map(&:id))
