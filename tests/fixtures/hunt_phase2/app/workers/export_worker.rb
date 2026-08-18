class ExportWorker
  include ApplicationWorker
  include Gitlab::SidekiqMiddleware

  sidekiq_options queue: :project_export
  feature_category :import_export
  urgency :low
  idempotent!

  def perform(user_id, project_id)
    [user_id, project_id]
  end
end
