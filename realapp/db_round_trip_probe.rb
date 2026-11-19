require "json"

iterations = Integer(ENV.fetch("DB_PROBE_ITERATIONS", "200"))
raise ArgumentError, "DB_PROBE_ITERATIONS must be 1 to 100000" unless (1..100_000).cover?(iterations)
values = ActiveRecord::Base.uncached do
  Array.new(iterations) do
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC, :nanosecond)
    ActiveRecord::Base.connection.select_value("SELECT 1")
    finished = Process.clock_gettime(Process::CLOCK_MONOTONIC, :nanosecond)
    (finished - started) / 1_000_000.0
  end
end

sorted = values.sort
puts JSON.generate({
  iterations: iterations,
  median_ms: sorted[(iterations - 1) / 2].round(4),
  p95_ms: sorted[(iterations * 95 / 100) - 1].round(4),
  mean_ms: (values.sum / iterations).round(4),
  samples_ms: values.map { |value| value.round(4) }
})
