class ProjectExportService
  def initialize(user, project)
    @user = user
    @project = project
  end

  def execute
    ExportWorker.perform_async(@user.id, @project.id)
  end

  def schedule
    ScheduledExportWorker.perform_in(5.minutes, @user.id, @project.id)
  end

  def enqueue_unknown
    UnknownExportWorker.perform_async(@user.id, @project.id)
  end

  def schedule_at
    ExportWorker.perform_at(1.hour.from_now, @user.id, @project.id)
  end

  def push_one
    Sidekiq::Client.push("class" => ExportWorker, "args" => [@user.id, @project.id])
  end

  def push_many
    Sidekiq::Client.push_bulk("class" => ExportWorker, "args" => [[@user.id, @project.id]])
  end

  def delayed_proxy
    ExportWorker.delay
  end
end

class NamespaceProbeService
  def initialize(user, project)
    @user = user
    @project = project
  end

  def execute
    ExportWorker.perform_async(@user.id, @project.id)
  end
end

class FilteredProbeService
  def initialize(user, project)
    @user = user
    @project = project
  end

  def execute
    ExportWorker.perform_async(@user.id, @project.id)
  end
end

module Api
  module V1
    class NamespaceProbeService
      def initialize(user, project)
        @user = user
        @project = project
      end

      def execute
        ScheduledExportWorker.perform_in(5.minutes, @user.id, @project.id)
      end
    end
  end
end

module QueueScope
  module Sidekiq
    class Client
    end
  end

  class ExportWorker
    include ApplicationWorker

    def perform(user_id, project_id)
      [user_id, project_id]
    end
  end

  class RelativeEnqueueService
    def execute(user_id, project_id)
      ExportWorker.perform_async(user_id, project_id)
    end

    def absolute_execute(user_id, project_id)
      ::ExportWorker.perform_async(user_id, project_id)
    end

    def shadowed_push(user_id, project_id)
      Sidekiq::Client.push("class" => ::ExportWorker, "args" => [user_id, project_id])
    end
  end
end

class QueueScope::ColonStyleEnqueueService
  def execute(user_id, project_id)
    ExportWorker.perform_async(user_id, project_id)
  end
end

class DecoyWorker
  Helper.include ApplicationWorker
  Helper.sidekiq_options queue: :must_not_be_indexed

  def perform(user_id)
    user_id
  end
end

class ReceiverDecoyWorker
  ReceiverDecoyWorker.include ApplicationWorker
  ReceiverDecoyWorker.sidekiq_options queue: :must_also_not_be_indexed

  def perform(user_id)
    user_id
  end
end
