module Legacy
  class ProjectExportsController < ApplicationController
    def retry
      authorize!(:export_project, current_project)
    end
  end
end
