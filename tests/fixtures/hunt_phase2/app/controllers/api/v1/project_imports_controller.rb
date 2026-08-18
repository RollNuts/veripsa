module Api
  module V1
    class ProjectImportsController < ApplicationController
      def create
        authorize!(:import_project, current_project)
      end

      def validate
        current_user.can?(:import_project, current_project)
      end
    end
  end
end
