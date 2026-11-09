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
