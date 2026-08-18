Rails.application.routes.draw do
  namespace :api do
    namespace :v1 do
      resources :project_exports, only: %i[create show update destroy] do
        member do
          get :download
          post :schedule
          post :unknown
          put :restart
          patch :rename
          delete :cancel
        end

        collection do
          get :status
          post :ambiguous
          post :namespace_shadow
          post :absolute_namespace
          post :filtered_shadow
          post :double_authorization
          post :dynamic_authorization
        end

        scope "/nested_scope" do
          get :nested_status
        end
      end

      resource :project_import, only: %i[create] do
        post :validate
      end
    end
  end

  scope "/legacy", module: "legacy" do
    post "project_exports/:id/retry", to: "project_exports#retry"
  end

  get dynamic_export_path, to: dynamic_controller_action
  resources :module_exports, module: :admin
  resources :custom_param_exports, param: :slug
  resources :dynamic_path_exports, path: dynamic_resources_path
  resources :dynamic_controller_exports, controller: dynamic_resources_controller

  namespace :dynamic_namespace, path: dynamic_namespace_path do
    get :status
  end

  scope "/dynamic_module", module: dynamic_scope_module do
    get "status", to: "exports#status"
  end

  scope dynamic_scope_path, module: "legacy" do
    get "dynamic_scope_status", to: "project_exports#retry"
  end

  namespace dynamic_namespace_name, path: "/fixed", module: "legacy" do
    get :dynamic_namespace_status
  end

  project_export_routes
  client.get "/not_a_rails_route", to: "project_exports#retry"

  draw do
    get "/bare_draw_must_not_resolve", to: "legacy/project_exports#retry"
  end
end
