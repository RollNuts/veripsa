module Api
  module V1
    class FilteredProbeService
      def execute
        HiddenExportWorker.perform_async
      end
    end
  end
end
