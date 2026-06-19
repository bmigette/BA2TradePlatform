import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Layout from './components/layout/Layout';
import Dashboard from './pages/Dashboard';
import Datasets from './pages/Datasets';
import DatasetDetails from './pages/DatasetDetails';
import Training from './pages/Training';
import JobDetails from './pages/JobDetails';
import Models from './pages/Models';
import ModelDetails from './pages/ModelDetails';
import Backtesting from './pages/Backtesting';
import Settings from './pages/Settings';
import SavedData from './pages/SavedData';
import Tools from './pages/Tools';
import CacheManagement from './pages/CacheManagement';

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="datasets" element={<Datasets />} />
          <Route path="datasets/:id" element={<DatasetDetails />} />
          <Route path="training" element={<Training />} />
          <Route path="training/:id" element={<JobDetails />} />
          <Route path="models" element={<Models />} />
          <Route path="models/:id" element={<ModelDetails />} />
          <Route path="backtesting" element={<Backtesting />} />
          <Route path="tools" element={<Tools />} />
          <Route path="saved-data" element={<SavedData />} />
          <Route path="cache" element={<CacheManagement />} />
          <Route path="settings" element={<Settings />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

export default App;
