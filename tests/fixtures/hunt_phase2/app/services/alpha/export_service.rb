module Alpha
  class ExportService
    def initialize(user, project)
      @user = user
      @project = project
    end

    def execute
      AlphaExportWorker.perform_async(@user.id, @project.id)
    end
  end
end
