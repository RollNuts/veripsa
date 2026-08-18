module Api
  module V1
    class ProjectExportsController < ApplicationController
      def create
        authorize!(:export_project, current_project)
        audit_label()
        ProjectExportService.new(current_user, current_project).execute
      end

      def schedule
        current_user.can?(:export_project, current_project)
        ProjectExportService.new(current_user, current_project).schedule
      end

      def unknown
        Ability.allowed?(current_user, :export_project, current_project)
        ProjectExportService.new(current_user, current_project).enqueue_unknown
      end

      def download
        authorize(current_project, :download?)
      end

      def status
        policy(current_project)
      end

      def ambiguous
        authorize!(:export_project, current_project)
        ExportService.new(current_user, current_project).execute
      end

      def namespace_shadow
        authorize!(:export_project, current_project)
        NamespaceProbeService.new(current_user, current_project).execute
      end

      def absolute_namespace
        authorize!(:export_project, current_project)
        ::NamespaceProbeService.new(current_user, current_project).execute
      end

      def filtered_shadow
        authorize!(:export_project, current_project)
        FilteredProbeService.new(current_user, current_project).execute
      end

      def double_authorization
        authorize!(:export_project, current_project); authorize!(:download_project, current_project)
        ProjectExportService.new(current_user, current_project).execute
      end

      def dynamic_authorization
        # AUTH_BODY_MARKER_MUST_NOT_APPEAR_OUTSIDE_THE_CALL_INDEX
        authorize!(params[:permission], resource_for(params[:resource_type]))
      end

      def oversized_receiver
        current_user.with_context("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa").can?(:export_project, current_project)
      end

      def restart
        current_user.cannot?(:restart_export, current_project)
      end

      def rename
        Ability.denied?(current_user, :rename_export, current_project)
      end

      def cancel
        cannot?(:cancel_export, current_project)
      end

      def show
        allowed?(current_user, :read_export, current_project)
      end

      def update
        pundit_authorize(current_project, :update?)
      end

      def destroy
        can?(:destroy_export, current_project)
      end
    end
  end
end
