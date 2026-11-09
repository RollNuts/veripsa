Rails.application.routes.draw do
  namespace :api do
    namespace :v2 do
      resources :workspaces, only: [] do
        scope module: :workspaces do
          resources :jobs, only: [:index, :create, :destroy]
          resource :quota, only: [:show]
          get :direct_action, on: :member
          namespace :uncomposed do
            resources :jobs, only: [:index]
          end
          resources :splatted, only: [:index], **route_options
          resources :duplicate, path: "one", path: "two"
          resources :containers, only: [] do
            resources :entries, path: "old", path: "actual", only: [:index]
            resources :splatted_entries, controller: "entries", only: [:index], **route_options
            resources :dynamic_entries, only: dynamic_actions do
              resources :discarded_dynamic, controller: "entries", only: [:index]
            end
            get :duplicate_to, to: "entries#index", to: "entries#missing", on: :collection
            get :splatted_to, to: "entries#index", on: :collection, **route_options
            get :unknown_options, to: "entries#index", on: :collection, constraints: route_constraints
            get :absolute_to, to: "/entries#index", on: :collection
            get :absolute_controller, controller: "/entries", action: :index, on: :collection
            get "absolute_rocket" => "/entries#index", on: :collection
            resources :absolute_entries, controller: "/entries", only: [:index]
            get :inline_endpoint, to: "entries#index", on: :collection do
              "not a controller endpoint"
            end
            get(:empty_inline_endpoint, to: "entries#index", on: :collection) {}
            member :ignored_argument do
              get :discarded_member, to: "entries#index"
            end
            collection(**route_options) do
              get :discarded_collection, to: "entries#index"
            end
            Rails.application.routes.draw do
              get :discarded_draw, to: "entries#index", on: :collection
            end
            scope "/unsupported_path" do
              get :discarded_scope, to: "entries#index", on: :collection
            end
            scope module: :nested, module: :wrong do
              resources :discarded_module, controller: "entries", only: [:index]
            end
            resources :valid_entries, controller: "entries", only: [:index] do
              resources :deeper_entries, controller: "entries", only: [:index]
              member do
                get :detail, to: "entries#index"
                get :dynamic_on, to: "entries#index", on: dynamic_route_scope
                get "rocket" => "entries#index"
              end
              collection do
                get :summary, to: "entries#index"
                get "rocket_summary" => "entries#index", on: :collection
              end
            end
            scope module: :nested do
              resources :deeper_entries, controller: "entries", only: [:index]
            end
          end
        end
        scope module: dynamic_module do
          resources :dynamic_jobs, only: [:index]
        end
        scope module: :workspaces, path: "/changed" do
          resources :path_jobs, only: [:index]
        end
        scope module: :workspaces, **route_options do
          resources :splat_jobs, only: [:index]
        end
        scope module: :workspaces, module: :other do
          resources :duplicate_jobs, only: [:index]
        end
        scope module: "/absolute" do
          resources :absolute_jobs, only: [:index]
        end
      end
      resource :workspace, only: [] do
        scope({ module: "workspaces" }) do
          resource :quota, only: [:show]
        end
      end
    end
  end
end
