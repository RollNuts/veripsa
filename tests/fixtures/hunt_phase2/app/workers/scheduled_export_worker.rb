class ScheduledExportWorker
  include Sidekiq::Worker

  sidekiq_options queue: :scheduled_project_export

  def perform(user_id, project_id)
    [user_id, project_id]
  end
end
