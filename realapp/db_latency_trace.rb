if ENV["DB_LATENCY_TRACE"] == "1"
  require "json"

  module DbLatencyTrace
    KEY = :db_latency_trace

    class Middleware
      def initialize(app)
        @app = app
      end

      def call(env)
        Thread.current[KEY] = { calls: 0, sql_ms: 0.0 }
        status, headers, body = @app.call(env)
        request = ActionDispatch::Request.new(env)
        path = request.path_parameters
        route = if path["controller"] && path["action"]
          [path["controller"], path["action"]].join("#")
        else
          request.path
        end
        state = Thread.current[KEY]
        Rails.logger.info(
          "DB_LATENCY_TRACE #{JSON.generate({
            route: route,
            status: status, uncached_sql_calls: state[:calls],
            sql_ms: state[:sql_ms].round(3)
          })}"
        )
        [status, headers, body]
      ensure
        Thread.current[KEY] = nil
      end
    end
  end

  ActiveSupport::Notifications.subscribe("sql.active_record") do |_name, started, finished, _id, payload|
    state = Thread.current[DbLatencyTrace::KEY]
    if state && !payload[:cached]
      state[:calls] += 1
      state[:sql_ms] += (finished - started) * 1000
    end
  end

  Rails.application.config.middleware.insert_before(0, DbLatencyTrace::Middleware)
end
